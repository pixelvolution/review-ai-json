<?php
/**
 * Registers the `pixel_item` custom post type and its meta.
 *
 * Items are full public posts: each one gets its own inner page (single view)
 * with fully built page content. Trending status is just meta that the
 * refresh endpoint flips on and off — items themselves persist.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_CPT {

	const POST_TYPE = 'pixel_item';

	/**
	 * Meta keys written by the refresh pipeline / admin toggles.
	 */
	const META_SKU              = '_pixel_sku';
	const META_TREND_SCORE      = '_pixel_trend_score';
	const META_TREND_REASON     = '_pixel_trend_reason';
	const META_TRENDING         = '_pixel_trending';
	const META_IMAGE_URL        = '_pixel_image_url';
	const META_IMAGE_SOURCE     = '_pixel_image_source_url';
	const META_EMBED_LINK       = '_pixel_embed_link';
	const META_PULLED_AT        = '_pixel_pulled_at';
	const META_HIDDEN           = '_pixel_hidden';
	const META_FEATURED         = '_pixel_featured';
	const META_CONTENT_HASH     = '_pixel_content_hash';

	public static function init() {
		add_action( 'init', array( __CLASS__, 'register' ) );
		add_filter( 'single_template', array( __CLASS__, 'single_template' ) );
	}

	public static function register() {
		register_post_type(
			self::POST_TYPE,
			array(
				'labels'       => array(
					'name'          => __( 'Items', 'pixel-trends' ),
					'singular_name' => __( 'Item', 'pixel-trends' ),
					'menu_name'     => __( 'Trend Items', 'pixel-trends' ),
					'add_new_item'  => __( 'Add New Item', 'pixel-trends' ),
					'edit_item'     => __( 'Edit Item', 'pixel-trends' ),
					'view_item'     => __( 'View Item', 'pixel-trends' ),
				),
				'public'       => true,
				'has_archive'  => true,
				'rewrite'      => array(
					'slug'       => apply_filters( 'pixel_trends_rewrite_slug', 'items' ),
					'with_front' => false,
				),
				'menu_icon'    => 'dashicons-chart-line',
				'supports'     => array( 'title', 'editor', 'thumbnail', 'excerpt', 'custom-fields' ),
				'show_in_rest' => true,
			)
		);

		$meta = array(
			self::META_SKU          => 'string',
			self::META_TREND_SCORE  => 'number',
			self::META_TREND_REASON => 'string',
			self::META_TRENDING     => 'string',
			self::META_IMAGE_URL    => 'string',
			self::META_EMBED_LINK   => 'string',
			self::META_PULLED_AT    => 'string',
			self::META_HIDDEN       => 'string',
			self::META_FEATURED     => 'string',
		);

		foreach ( $meta as $key => $type ) {
			register_post_meta(
				self::POST_TYPE,
				$key,
				array(
					'type'          => $type,
					'single'        => true,
					'show_in_rest'  => false,
					'auth_callback' => static function () {
						return current_user_can( 'edit_posts' );
					},
				)
			);
		}
	}

	/**
	 * Use the theme's single-pixel_item.php when it exists, otherwise fall
	 * back to the plugin template so inner pages render fully out of the box.
	 */
	public static function single_template( $template ) {
		if ( is_singular( self::POST_TYPE ) ) {
			$theme_template = locate_template( array( 'single-' . self::POST_TYPE . '.php' ) );
			if ( $theme_template ) {
				return $theme_template;
			}
			return PIXEL_TRENDS_DIR . 'templates/single-pixel-item.php';
		}
		return $template;
	}
}
