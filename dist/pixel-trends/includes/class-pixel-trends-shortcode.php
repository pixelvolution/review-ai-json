<?php
/**
 * [pixel_trending] shortcode — renders the current trending items as a card
 * grid. Cards link to each item's fully built inner page by default; pass
 * link="embed" to link straight out to the Dutchie/Leafly listing instead.
 *
 * [pixel_trend_deals] is kept as an alias for the original spec name.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_Shortcode {

	public static function init() {
		add_shortcode( 'pixel_trending', array( __CLASS__, 'render' ) );
		add_shortcode( 'pixel_trend_deals', array( __CLASS__, 'render' ) );

		add_action(
			'wp_enqueue_scripts',
			static function () {
				wp_register_style(
					'pixel-trends',
					PIXEL_TRENDS_URL . 'assets/css/pixel-trends.css',
					array(),
					PIXEL_TRENDS_VERSION
				);

				// Inner pages always need the styles; the shortcode enqueues
				// on demand elsewhere.
				if ( is_singular( Pixel_Trends_CPT::POST_TYPE ) ) {
					wp_enqueue_style( 'pixel-trends' );
				}
			}
		);
	}

	public static function render( $atts ) {
		$atts = shortcode_atts(
			array(
				'count'       => 6,
				'columns'     => 3,
				'link'        => 'page', // "page" (inner page) or "embed" (menu listing).
				'show_reason' => 'yes',
			),
			$atts,
			'pixel_trending'
		);

		$count   = max( 1, (int) $atts['count'] );
		$columns = min( 6, max( 1, (int) $atts['columns'] ) );

		$query = new WP_Query(
			array(
				'post_type'      => Pixel_Trends_CPT::POST_TYPE,
				'post_status'    => 'publish',
				'posts_per_page' => $count,
				'no_found_rows'  => true,
				'meta_query'     => array(
					'relation' => 'AND',
					array(
						'key'   => Pixel_Trends_CPT::META_TRENDING,
						'value' => '1',
					),
					array(
						'key'     => Pixel_Trends_CPT::META_HIDDEN,
						'value'   => '1',
						'compare' => '!=',
					),
					'featured' => array(
						'key'  => Pixel_Trends_CPT::META_FEATURED,
						'type' => 'NUMERIC',
					),
					'score'    => array(
						'key'  => Pixel_Trends_CPT::META_TREND_SCORE,
						'type' => 'NUMERIC',
					),
				),
				'orderby'        => array(
					'featured' => 'DESC',
					'score'    => 'DESC',
				),
			)
		);

		if ( ! $query->have_posts() ) {
			return '';
		}

		wp_enqueue_style( 'pixel-trends' );

		ob_start();

		echo '<div class="pixel-trending-grid pixel-trending-cols-' . esc_attr( $columns ) . '">';

		while ( $query->have_posts() ) {
			$query->the_post();
			self::render_card( get_the_ID(), $atts );
		}

		echo '</div>';

		wp_reset_postdata();

		return ob_get_clean();
	}

	private static function render_card( $post_id, array $atts ) {
		$embed_link = get_post_meta( $post_id, Pixel_Trends_CPT::META_EMBED_LINK, true );
		$reason     = get_post_meta( $post_id, Pixel_Trends_CPT::META_TREND_REASON, true );

		$url      = get_permalink( $post_id );
		$external = false;

		if ( 'embed' === $atts['link'] && $embed_link ) {
			$url      = $embed_link;
			$external = true;
		}

		$image = '';
		if ( has_post_thumbnail( $post_id ) ) {
			$image = get_the_post_thumbnail( $post_id, 'medium_large', array( 'class' => 'pixel-trending-card-image' ) );
		} else {
			$image_url = get_post_meta( $post_id, Pixel_Trends_CPT::META_IMAGE_URL, true );
			if ( $image_url ) {
				$image = sprintf(
					'<img class="pixel-trending-card-image" src="%s" alt="%s" loading="lazy" />',
					esc_url( $image_url ),
					esc_attr( get_the_title( $post_id ) )
				);
			}
		}

		?>
		<a
			class="pixel-trending-card"
			href="<?php echo esc_url( $url ); ?>"
			<?php echo $external ? 'target="_blank" rel="noopener nofollow"' : ''; ?>
		>
			<div class="pixel-trending-card-media">
				<?php echo $image; // Escaped above. ?>
				<?php if ( 'yes' === $atts['show_reason'] && $reason ) : ?>
					<span class="pixel-trending-card-badge"><?php echo esc_html( $reason ); ?></span>
				<?php endif; ?>
			</div>
			<div class="pixel-trending-card-body">
				<h3 class="pixel-trending-card-title"><?php echo esc_html( get_the_title( $post_id ) ); ?></h3>
				<?php if ( has_excerpt( $post_id ) ) : ?>
					<p class="pixel-trending-card-excerpt"><?php echo esc_html( get_the_excerpt( $post_id ) ); ?></p>
				<?php endif; ?>
			</div>
		</a>
		<?php
	}
}
