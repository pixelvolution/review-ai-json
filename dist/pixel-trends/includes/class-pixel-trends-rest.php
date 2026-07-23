<?php
/**
 * REST controller — the plugin's entire server-facing surface.
 *
 * POST /wp-json/pixel-trends/v1/refresh  — upsert the item catalog + trending flags.
 * GET  /wp-json/pixel-trends/v1/status   — last run summary for the central monitor.
 *
 * No cron runs inside WordPress; both routes only respond to authenticated
 * calls from the central Worker (token header + HMAC-signed request).
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_REST {

	const NS = 'pixel-trends/v1';

	/** Maximum allowed clock skew for signed requests, in seconds. */
	const TIMESTAMP_TOLERANCE = 300;

	public static function init() {
		add_action( 'rest_api_init', array( __CLASS__, 'register_routes' ) );
	}

	public static function register_routes() {
		register_rest_route(
			self::NS,
			'/refresh',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'callback'            => array( __CLASS__, 'handle_refresh' ),
				'permission_callback' => array( __CLASS__, 'authenticate' ),
			)
		);

		register_rest_route(
			self::NS,
			'/status',
			array(
				'methods'             => WP_REST_Server::READABLE,
				'callback'            => array( __CLASS__, 'handle_status' ),
				'permission_callback' => array( __CLASS__, 'authenticate' ),
			)
		);
	}

	/**
	 * Token + HMAC authentication.
	 *
	 * Required headers:
	 *   X-Pixel-Token:     the site token issued at activation.
	 *   X-Pixel-Timestamp: unix timestamp (rejected outside a 5-minute window).
	 *   X-Pixel-Signature: hex HMAC-SHA256 of "{timestamp}.{raw body}" keyed
	 *                      with the site secret.
	 *
	 * A leaked token alone is not replayable: without the secret the caller
	 * cannot produce a valid signature, and stale signatures expire.
	 */
	public static function authenticate( WP_REST_Request $request ) {
		$token  = get_option( 'pixel_trends_token' );
		$secret = get_option( 'pixel_trends_secret' );

		if ( ! $token || ! $secret ) {
			return new WP_Error(
				'pixel_trends_not_configured',
				__( 'Site credentials are not configured.', 'pixel-trends' ),
				array( 'status' => 503 )
			);
		}

		$provided_token = (string) $request->get_header( 'x-pixel-token' );
		$timestamp      = (string) $request->get_header( 'x-pixel-timestamp' );
		$signature      = strtolower( (string) $request->get_header( 'x-pixel-signature' ) );

		$forbidden = new WP_Error(
			'pixel_trends_forbidden',
			__( 'Invalid credentials.', 'pixel-trends' ),
			array( 'status' => 403 )
		);

		if ( '' === $provided_token || ! hash_equals( $token, $provided_token ) ) {
			return $forbidden;
		}

		if ( '' === $timestamp || '' === $signature ) {
			return $forbidden;
		}

		if ( abs( time() - (int) $timestamp ) > self::TIMESTAMP_TOLERANCE ) {
			return $forbidden;
		}

		$expected = hash_hmac( 'sha256', $timestamp . '.' . $request->get_body(), $secret );
		if ( ! hash_equals( $expected, $signature ) ) {
			return $forbidden;
		}

		return true;
	}

	/**
	 * Upsert the catalog and set the current trending picks.
	 *
	 * Payload:
	 * {
	 *   "items": [
	 *     {
	 *       "sku": "KOVA-1234",              // required — stable upsert key
	 *       "name": "Blue Dream 3.5g",       // required
	 *       "trending": true,                // in today's trending set
	 *       "trend_score": 87.4,
	 *       "trend_reason": "moving fast",
	 *       "image_url": "https://…",
	 *       "embed_link": "https://dutchie.com/…",
	 *       "excerpt": "Short card text",
	 *       "description_html": "<p>…</p>",  // or "description" (plain text)
	 *       "details": {"Brand": "…", "THC": "24%"}
	 *     }
	 *   ],
	 *   "replace_trending": true             // default true: items absent from
	 * }                                      // this payload's trending set are
	 *                                        // un-flagged
	 */
	public static function handle_refresh( WP_REST_Request $request ) {
		$payload = $request->get_json_params();
		$items   = isset( $payload['items'] ) && is_array( $payload['items'] ) ? $payload['items'] : null;

		if ( null === $items ) {
			return new WP_Error(
				'pixel_trends_bad_payload',
				__( 'Payload must contain an "items" array.', 'pixel-trends' ),
				array( 'status' => 400 )
			);
		}

		$replace_trending = ! isset( $payload['replace_trending'] ) || (bool) $payload['replace_trending'];
		$pulled_at        = gmdate( 'c' );

		$created      = 0;
		$updated      = 0;
		$skipped      = 0;
		$trending_ids = array();

		foreach ( $items as $item ) {
			if ( ! is_array( $item ) ) {
				$skipped++;
				continue;
			}

			$result = self::upsert_item( $item, $pulled_at );

			if ( null === $result ) {
				$skipped++;
				continue;
			}

			list( $post_id, $is_new ) = $result;
			$is_new ? $created++ : $updated++;

			if ( ! empty( $item['trending'] ) ) {
				$trending_ids[] = $post_id;
			}
		}

		if ( $replace_trending ) {
			self::replace_trending_set( $trending_ids );
		} else {
			foreach ( $trending_ids as $post_id ) {
				update_post_meta( $post_id, Pixel_Trends_CPT::META_TRENDING, '1' );
			}
		}

		$summary = array(
			'status'    => 'success',
			'created'   => $created,
			'updated'   => $updated,
			'skipped'   => $skipped,
			'trending'  => count( $trending_ids ),
			'pulled_at' => $pulled_at,
		);

		update_option( 'pixel_trends_last_run', $summary, false );

		return rest_ensure_response( $summary );
	}

	public static function handle_status() {
		$last_run = get_option( 'pixel_trends_last_run' );

		return rest_ensure_response(
			array(
				'plugin_version' => PIXEL_TRENDS_VERSION,
				'last_run'       => $last_run ? $last_run : null,
			)
		);
	}

	/**
	 * Create or update the item post keyed by SKU.
	 *
	 * @return array{0:int,1:bool}|null [post_id, is_new] or null when skipped.
	 */
	private static function upsert_item( array $item, $pulled_at ) {
		$sku  = isset( $item['sku'] ) ? sanitize_text_field( (string) $item['sku'] ) : '';
		$name = isset( $item['name'] ) ? sanitize_text_field( (string) $item['name'] ) : '';

		if ( '' === $sku || '' === $name ) {
			return null;
		}

		$existing = get_posts(
			array(
				'post_type'      => Pixel_Trends_CPT::POST_TYPE,
				'post_status'    => 'any',
				'posts_per_page' => 1,
				'fields'         => 'ids',
				'meta_key'       => Pixel_Trends_CPT::META_SKU,
				'meta_value'     => $sku,
				'no_found_rows'  => true,
			)
		);

		$post_id = $existing ? (int) $existing[0] : 0;
		$is_new  = 0 === $post_id;

		$postarr = array(
			'post_type'   => Pixel_Trends_CPT::POST_TYPE,
			'post_status' => 'publish',
			'post_title'  => $name,
		);

		if ( isset( $item['excerpt'] ) ) {
			$postarr['post_excerpt'] = sanitize_text_field( (string) $item['excerpt'] );
		}

		if ( $is_new ) {
			$post_id = wp_insert_post( $postarr, true );
			if ( is_wp_error( $post_id ) ) {
				return null;
			}
		} else {
			$postarr['ID'] = $post_id;
			// Don't clobber a hand-tuned title.
			unset( $postarr['post_title'] );
			wp_update_post( $postarr );
		}

		// Fully built inner page — regenerated on every refresh unless the
		// owner hand-edited the page since the builder last wrote it.
		if ( Pixel_Trends_Content_Builder::can_overwrite( $post_id ) ) {
			$content = Pixel_Trends_Content_Builder::build( $item );
			if ( '' !== $content ) {
				wp_update_post(
					array(
						'ID'           => $post_id,
						'post_content' => $content,
					)
				);
				Pixel_Trends_Content_Builder::remember( $post_id, $content );
			}
		}

		update_post_meta( $post_id, Pixel_Trends_CPT::META_SKU, $sku );
		update_post_meta( $post_id, Pixel_Trends_CPT::META_PULLED_AT, $pulled_at );
		update_post_meta(
			$post_id,
			Pixel_Trends_CPT::META_TREND_SCORE,
			isset( $item['trend_score'] ) ? (float) $item['trend_score'] : 0
		);
		update_post_meta(
			$post_id,
			Pixel_Trends_CPT::META_TREND_REASON,
			isset( $item['trend_reason'] ) ? sanitize_text_field( (string) $item['trend_reason'] ) : ''
		);
		update_post_meta(
			$post_id,
			Pixel_Trends_CPT::META_EMBED_LINK,
			isset( $item['embed_link'] ) ? esc_url_raw( (string) $item['embed_link'] ) : ''
		);

		$image_url = isset( $item['image_url'] ) ? esc_url_raw( (string) $item['image_url'] ) : '';
		update_post_meta( $post_id, Pixel_Trends_CPT::META_IMAGE_URL, $image_url );
		self::maybe_sideload_image( $post_id, $image_url );

		// Ensure toggle meta always exists so shortcode meta queries stay simple.
		if ( '' === get_post_meta( $post_id, Pixel_Trends_CPT::META_HIDDEN, true ) ) {
			update_post_meta( $post_id, Pixel_Trends_CPT::META_HIDDEN, '0' );
		}
		if ( '' === get_post_meta( $post_id, Pixel_Trends_CPT::META_FEATURED, true ) ) {
			update_post_meta( $post_id, Pixel_Trends_CPT::META_FEATURED, '0' );
		}

		return array( $post_id, $is_new );
	}

	/**
	 * Make $trending_ids the complete current trending set.
	 */
	private static function replace_trending_set( array $trending_ids ) {
		$currently_trending = get_posts(
			array(
				'post_type'      => Pixel_Trends_CPT::POST_TYPE,
				'post_status'    => 'any',
				'posts_per_page' => -1,
				'fields'         => 'ids',
				'meta_key'       => Pixel_Trends_CPT::META_TRENDING,
				'meta_value'     => '1',
				'no_found_rows'  => true,
			)
		);

		foreach ( $currently_trending as $post_id ) {
			if ( ! in_array( (int) $post_id, $trending_ids, true ) ) {
				update_post_meta( $post_id, Pixel_Trends_CPT::META_TRENDING, '0' );
			}
		}

		foreach ( $trending_ids as $post_id ) {
			update_post_meta( $post_id, Pixel_Trends_CPT::META_TRENDING, '1' );
		}
	}

	/**
	 * Download the menu image into the media library and set it as the
	 * featured image. Skipped when the same source URL was already imported.
	 */
	private static function maybe_sideload_image( $post_id, $image_url ) {
		if ( '' === $image_url ) {
			return;
		}

		$previous = get_post_meta( $post_id, Pixel_Trends_CPT::META_IMAGE_SOURCE, true );
		if ( $previous === $image_url && has_post_thumbnail( $post_id ) ) {
			return;
		}

		require_once ABSPATH . 'wp-admin/includes/media.php';
		require_once ABSPATH . 'wp-admin/includes/file.php';
		require_once ABSPATH . 'wp-admin/includes/image.php';

		$attachment_id = media_sideload_image( $image_url, $post_id, get_the_title( $post_id ), 'id' );

		if ( ! is_wp_error( $attachment_id ) ) {
			set_post_thumbnail( $post_id, $attachment_id );
			update_post_meta( $post_id, Pixel_Trends_CPT::META_IMAGE_SOURCE, $image_url );
		}
	}
}
