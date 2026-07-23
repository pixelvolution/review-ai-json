<?php
/**
 * Builds the fully rendered inner-page content for an item from the
 * refresh payload: description, details table, and menu link.
 *
 * The builder only ever writes content it "owns". A hash of the generated
 * content is stored alongside the post; if the stored hash no longer matches
 * the live post content, the owner (or their designer) has hand-edited the
 * page and the builder leaves it alone on subsequent refreshes.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_Content_Builder {

	/**
	 * Build the inner-page HTML for an item payload.
	 *
	 * @param array $item Item payload from the refresh endpoint.
	 * @return string Sanitized post content.
	 */
	public static function build( array $item ) {
		$sections = array();

		if ( ! empty( $item['description_html'] ) ) {
			$sections[] = wp_kses_post( $item['description_html'] );
		} elseif ( ! empty( $item['description'] ) ) {
			$sections[] = wpautop( esc_html( $item['description'] ) );
		}

		if ( ! empty( $item['details'] ) && is_array( $item['details'] ) ) {
			$rows = '';
			foreach ( $item['details'] as $label => $value ) {
				if ( ! is_scalar( $value ) ) {
					continue;
				}
				$rows .= sprintf(
					'<tr><th scope="row">%s</th><td>%s</td></tr>',
					esc_html( (string) $label ),
					esc_html( (string) $value )
				);
			}
			if ( $rows ) {
				$sections[] = '<figure class="pixel-item-details"><table><tbody>' . $rows . '</tbody></table></figure>';
			}
		}

		if ( ! empty( $item['embed_link'] ) ) {
			$sections[] = sprintf(
				'<p class="pixel-item-menu-link"><a class="pixel-item-button" href="%s" target="_blank" rel="noopener nofollow">%s</a></p>',
				esc_url( $item['embed_link'] ),
				esc_html( apply_filters( 'pixel_trends_menu_link_label', __( 'View on our live menu', 'pixel-trends' ) ) )
			);
		}

		$content = implode( "\n\n", $sections );

		/**
		 * Filter the generated inner-page content before it is saved.
		 *
		 * @param string $content Generated HTML.
		 * @param array  $item    Raw item payload.
		 */
		return apply_filters( 'pixel_trends_item_content', $content, $item );
	}

	/**
	 * Whether the builder may overwrite the post's current content.
	 *
	 * True for new posts and for posts whose content still matches what the
	 * builder last wrote (i.e. never hand-edited).
	 */
	public static function can_overwrite( $post_id ) {
		$stored_hash = get_post_meta( $post_id, Pixel_Trends_CPT::META_CONTENT_HASH, true );
		if ( '' === $stored_hash ) {
			// Never built before: only overwrite if the post has no content yet.
			return '' === trim( (string) get_post_field( 'post_content', $post_id ) );
		}
		$current = (string) get_post_field( 'post_content', $post_id );
		return hash_equals( $stored_hash, md5( $current ) );
	}

	/**
	 * Record the hash of the content the builder just wrote.
	 */
	public static function remember( $post_id, $content ) {
		update_post_meta( $post_id, Pixel_Trends_CPT::META_CONTENT_HASH, md5( (string) $content ) );
	}
}
