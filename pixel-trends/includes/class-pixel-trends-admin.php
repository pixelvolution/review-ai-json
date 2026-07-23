<?php
/**
 * Admin surface — deliberately tiny:
 *
 *  - "Today's Picks": the current trending list with per-item Hide/Feature
 *    toggles. No raw POS data is ever shown.
 *  - "Settings": the site credentials the central Worker uses, with
 *    regenerate buttons, plus the shortcode with a copy-ready field.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_Admin {

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'register_menu' ) );
		add_action( 'admin_post_pixel_trends_toggle', array( __CLASS__, 'handle_toggle' ) );
		add_action( 'admin_post_pixel_trends_regenerate', array( __CLASS__, 'handle_regenerate' ) );
	}

	public static function register_menu() {
		$parent = 'edit.php?post_type=' . Pixel_Trends_CPT::POST_TYPE;

		add_submenu_page(
			$parent,
			__( "Today's Picks", 'pixel-trends' ),
			__( "Today's Picks", 'pixel-trends' ),
			'edit_posts',
			'pixel-trends-picks',
			array( __CLASS__, 'render_picks_page' )
		);

		add_submenu_page(
			$parent,
			__( 'Trend Settings', 'pixel-trends' ),
			__( 'Settings', 'pixel-trends' ),
			'manage_options',
			'pixel-trends-settings',
			array( __CLASS__, 'render_settings_page' )
		);
	}

	public static function render_picks_page() {
		$last_run = get_option( 'pixel_trends_last_run' );

		$picks = get_posts(
			array(
				'post_type'      => Pixel_Trends_CPT::POST_TYPE,
				'post_status'    => 'publish',
				'posts_per_page' => 50,
				'no_found_rows'  => true,
				'meta_key'       => Pixel_Trends_CPT::META_TREND_SCORE,
				'orderby'        => 'meta_value_num',
				'order'          => 'DESC',
				'meta_query'     => array(
					array(
						'key'   => Pixel_Trends_CPT::META_TRENDING,
						'value' => '1',
					),
				),
			)
		);
		?>
		<div class="wrap">
			<h1><?php esc_html_e( "Today's Picks", 'pixel-trends' ); ?></h1>

			<?php if ( $last_run ) : ?>
				<p>
					<?php
					printf(
						/* translators: 1: timestamp, 2: item count */
						esc_html__( 'Last refresh: %1$s — %2$d trending items.', 'pixel-trends' ),
						esc_html( $last_run['pulled_at'] ),
						(int) $last_run['trending']
					);
					?>
				</p>
			<?php else : ?>
				<p><?php esc_html_e( 'No refresh has run yet. Your picks will appear here after the first morning update.', 'pixel-trends' ); ?></p>
			<?php endif; ?>

			<?php if ( $picks ) : ?>
				<table class="widefat striped">
					<thead>
						<tr>
							<th><?php esc_html_e( 'Item', 'pixel-trends' ); ?></th>
							<th><?php esc_html_e( 'Why it’s trending', 'pixel-trends' ); ?></th>
							<th><?php esc_html_e( 'Score', 'pixel-trends' ); ?></th>
							<th><?php esc_html_e( 'Visibility', 'pixel-trends' ); ?></th>
							<th><?php esc_html_e( 'Featured', 'pixel-trends' ); ?></th>
						</tr>
					</thead>
					<tbody>
						<?php foreach ( $picks as $pick ) : ?>
							<?php
							$hidden   = '1' === get_post_meta( $pick->ID, Pixel_Trends_CPT::META_HIDDEN, true );
							$featured = '1' === get_post_meta( $pick->ID, Pixel_Trends_CPT::META_FEATURED, true );
							?>
							<tr>
								<td>
									<a href="<?php echo esc_url( get_edit_post_link( $pick->ID ) ); ?>">
										<strong><?php echo esc_html( get_the_title( $pick ) ); ?></strong>
									</a>
									<br />
									<a href="<?php echo esc_url( get_permalink( $pick ) ); ?>" target="_blank">
										<?php esc_html_e( 'View page', 'pixel-trends' ); ?>
									</a>
								</td>
								<td><?php echo esc_html( get_post_meta( $pick->ID, Pixel_Trends_CPT::META_TREND_REASON, true ) ); ?></td>
								<td><?php echo esc_html( get_post_meta( $pick->ID, Pixel_Trends_CPT::META_TREND_SCORE, true ) ); ?></td>
								<td>
									<?php
									self::toggle_link(
										$pick->ID,
										Pixel_Trends_CPT::META_HIDDEN,
										$hidden,
										__( 'Hidden — click to show', 'pixel-trends' ),
										__( 'Visible — click to hide', 'pixel-trends' )
									);
									?>
								</td>
								<td>
									<?php
									self::toggle_link(
										$pick->ID,
										Pixel_Trends_CPT::META_FEATURED,
										$featured,
										__( 'Featured — click to unfeature', 'pixel-trends' ),
										__( 'Click to feature first', 'pixel-trends' )
									);
									?>
								</td>
							</tr>
						<?php endforeach; ?>
					</tbody>
				</table>
			<?php endif; ?>
		</div>
		<?php
	}

	private static function toggle_link( $post_id, $meta_key, $is_on, $on_label, $off_label ) {
		$url = wp_nonce_url(
			add_query_arg(
				array(
					'action' => 'pixel_trends_toggle',
					'post'   => $post_id,
					'meta'   => $meta_key,
				),
				admin_url( 'admin-post.php' )
			),
			'pixel_trends_toggle_' . $post_id . '_' . $meta_key
		);

		printf(
			'<a href="%s">%s</a>',
			esc_url( $url ),
			esc_html( $is_on ? $on_label : $off_label )
		);
	}

	public static function handle_toggle() {
		$post_id  = isset( $_GET['post'] ) ? (int) $_GET['post'] : 0;
		$meta_key = isset( $_GET['meta'] ) ? sanitize_text_field( wp_unslash( $_GET['meta'] ) ) : '';

		$allowed = array( Pixel_Trends_CPT::META_HIDDEN, Pixel_Trends_CPT::META_FEATURED );

		if ( ! $post_id || ! in_array( $meta_key, $allowed, true ) ) {
			wp_die( esc_html__( 'Invalid toggle request.', 'pixel-trends' ) );
		}

		check_admin_referer( 'pixel_trends_toggle_' . $post_id . '_' . $meta_key );

		if ( ! current_user_can( 'edit_post', $post_id ) ) {
			wp_die( esc_html__( 'You are not allowed to edit this item.', 'pixel-trends' ) );
		}

		$current = get_post_meta( $post_id, $meta_key, true );
		update_post_meta( $post_id, $meta_key, '1' === $current ? '0' : '1' );

		wp_safe_redirect(
			admin_url( 'edit.php?post_type=' . Pixel_Trends_CPT::POST_TYPE . '&page=pixel-trends-picks' )
		);
		exit;
	}

	public static function render_settings_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$token  = get_option( 'pixel_trends_token', '' );
		$secret = get_option( 'pixel_trends_secret', '' );
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Trend Settings', 'pixel-trends' ); ?></h1>

			<h2><?php esc_html_e( 'Shortcode', 'pixel-trends' ); ?></h2>
			<p><?php esc_html_e( 'Paste this anywhere you want the trending grid (homepage, landing page, sidebar):', 'pixel-trends' ); ?></p>
			<p><input type="text" class="regular-text code" readonly value="[pixel_trending count=&quot;6&quot;]" onfocus="this.select();" /></p>
			<p class="description">
				<?php esc_html_e( 'Options: count, columns, link="page|embed", show_reason="yes|no". Cards link to each item’s full inner page by default.', 'pixel-trends' ); ?>
			</p>

			<h2><?php esc_html_e( 'Connection credentials', 'pixel-trends' ); ?></h2>
			<p><?php esc_html_e( 'These identify this site to the Pixelvolution refresh service. Regenerating them requires re-registering the site with the service.', 'pixel-trends' ); ?></p>

			<table class="form-table" role="presentation">
				<tr>
					<th scope="row"><?php esc_html_e( 'Site token', 'pixel-trends' ); ?></th>
					<td><input type="text" class="large-text code" readonly value="<?php echo esc_attr( $token ); ?>" onfocus="this.select();" /></td>
				</tr>
				<tr>
					<th scope="row"><?php esc_html_e( 'Signing secret', 'pixel-trends' ); ?></th>
					<td><input type="text" class="large-text code" readonly value="<?php echo esc_attr( $secret ); ?>" onfocus="this.select();" /></td>
				</tr>
			</table>

			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="pixel_trends_regenerate" />
				<?php wp_nonce_field( 'pixel_trends_regenerate' ); ?>
				<?php submit_button( __( 'Regenerate credentials', 'pixel-trends' ), 'secondary' ); ?>
			</form>

			<h2><?php esc_html_e( 'Refresh endpoint', 'pixel-trends' ); ?></h2>
			<p><code><?php echo esc_html( rest_url( Pixel_Trends_REST::NS . '/refresh' ) ); ?></code></p>
		</div>
		<?php
	}

	public static function handle_regenerate() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You are not allowed to do that.', 'pixel-trends' ) );
		}

		check_admin_referer( 'pixel_trends_regenerate' );

		update_option( 'pixel_trends_token', wp_generate_password( 48, false, false ), false );
		update_option( 'pixel_trends_secret', wp_generate_password( 64, false, false ), false );

		wp_safe_redirect(
			admin_url( 'edit.php?post_type=' . Pixel_Trends_CPT::POST_TYPE . '&page=pixel-trends-settings' )
		);
		exit;
	}
}
