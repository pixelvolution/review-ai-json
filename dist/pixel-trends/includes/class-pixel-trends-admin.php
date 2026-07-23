<?php
/**
 * Admin surface — deliberately tiny, styled as a glass dashboard:
 *
 *  - "Today's Picks": stat cards + the current trending list with per-item
 *    Hide/Feature toggles. No raw POS data is ever shown.
 *  - "Settings": the site credentials the central Worker uses, with
 *    regenerate buttons, plus the shortcode with a copy-ready field.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Pixel_Trends_Admin {

	/** Hook suffixes of our screens, used to scope the stylesheet. */
	private static $hooks = array();

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'register_menu' ) );
		add_action( 'admin_enqueue_scripts', array( __CLASS__, 'enqueue' ) );
		add_action( 'admin_post_pixel_trends_toggle', array( __CLASS__, 'handle_toggle' ) );
		add_action( 'admin_post_pixel_trends_regenerate', array( __CLASS__, 'handle_regenerate' ) );
	}

	public static function register_menu() {
		$parent = 'edit.php?post_type=' . Pixel_Trends_CPT::POST_TYPE;

		self::$hooks[] = add_submenu_page(
			$parent,
			__( "Today's Picks", 'pixel-trends' ),
			__( "Today's Picks", 'pixel-trends' ),
			'edit_posts',
			'pixel-trends-picks',
			array( __CLASS__, 'render_picks_page' )
		);

		self::$hooks[] = add_submenu_page(
			$parent,
			__( 'Trend Settings', 'pixel-trends' ),
			__( 'Settings', 'pixel-trends' ),
			'manage_options',
			'pixel-trends-settings',
			array( __CLASS__, 'render_settings_page' )
		);
	}

	public static function enqueue( $hook ) {
		if ( in_array( $hook, self::$hooks, true ) ) {
			wp_enqueue_style(
				'pixel-trends-admin',
				PIXEL_TRENDS_URL . 'assets/css/pixel-trends-admin.css',
				array(),
				PIXEL_TRENDS_VERSION
			);
		}
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

		$hidden_count   = 0;
		$featured_count = 0;
		foreach ( $picks as $pick ) {
			if ( '1' === get_post_meta( $pick->ID, Pixel_Trends_CPT::META_HIDDEN, true ) ) {
				$hidden_count++;
			}
			if ( '1' === get_post_meta( $pick->ID, Pixel_Trends_CPT::META_FEATURED, true ) ) {
				$featured_count++;
			}
		}
		?>
		<div class="wrap pixel-dash">
			<header class="pixel-dash-header">
				<h1><?php esc_html_e( "Today's Picks", 'pixel-trends' ); ?></h1>
				<p class="pixel-dash-sub">
					<?php
					if ( $last_run && ! empty( $last_run['pulled_at'] ) ) {
						printf(
							/* translators: %s: human-readable time difference */
							esc_html__( 'Last refresh %s ago', 'pixel-trends' ),
							esc_html( human_time_diff( strtotime( $last_run['pulled_at'] ) ) )
						);
					} else {
						esc_html_e( 'Waiting for the first morning refresh', 'pixel-trends' );
					}
					?>
				</p>
			</header>

			<div class="pixel-dash-stats">
				<div class="pixel-glass pixel-stat">
					<span class="pixel-stat-value"><?php echo (int) count( $picks ); ?></span>
					<span class="pixel-stat-label"><?php esc_html_e( 'Trending now', 'pixel-trends' ); ?></span>
				</div>
				<div class="pixel-glass pixel-stat">
					<span class="pixel-stat-value"><?php echo (int) $featured_count; ?></span>
					<span class="pixel-stat-label"><?php esc_html_e( 'Featured by you', 'pixel-trends' ); ?></span>
				</div>
				<div class="pixel-glass pixel-stat">
					<span class="pixel-stat-value"><?php echo (int) $hidden_count; ?></span>
					<span class="pixel-stat-label"><?php esc_html_e( 'Hidden by you', 'pixel-trends' ); ?></span>
				</div>
				<div class="pixel-glass pixel-stat">
					<span class="pixel-stat-value">
						<?php echo $last_run ? (int) $last_run['created'] + (int) $last_run['updated'] : 0; ?>
					</span>
					<span class="pixel-stat-label"><?php esc_html_e( 'Items in last sync', 'pixel-trends' ); ?></span>
				</div>
			</div>

			<div class="pixel-glass pixel-dash-panel">
				<?php if ( $picks ) : ?>
					<table class="pixel-dash-table">
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
								$reason   = get_post_meta( $pick->ID, Pixel_Trends_CPT::META_TREND_REASON, true );
								?>
								<tr class="<?php echo $hidden ? 'is-hidden-row' : ''; ?>">
									<td>
										<div class="pixel-item-cell">
											<span class="pixel-item-thumb">
												<?php echo get_the_post_thumbnail( $pick->ID, 'thumbnail' ); ?>
											</span>
											<span>
												<a href="<?php echo esc_url( get_edit_post_link( $pick->ID ) ); ?>">
													<?php echo esc_html( get_the_title( $pick ) ); ?>
												</a>
												<a class="pixel-item-view" href="<?php echo esc_url( get_permalink( $pick ) ); ?>" target="_blank">
													<?php esc_html_e( 'View page ↗', 'pixel-trends' ); ?>
												</a>
											</span>
										</div>
									</td>
									<td>
										<?php if ( $reason ) : ?>
											<span class="pixel-reason-badge"><?php echo esc_html( $reason ); ?></span>
										<?php endif; ?>
									</td>
									<td>
										<span class="pixel-score">
											<?php echo esc_html( get_post_meta( $pick->ID, Pixel_Trends_CPT::META_TREND_SCORE, true ) ); ?>
										</span>
									</td>
									<td>
										<?php
										self::toggle_pill(
											$pick->ID,
											Pixel_Trends_CPT::META_HIDDEN,
											$hidden,
											__( 'Hidden', 'pixel-trends' ),
											__( 'Visible', 'pixel-trends' ),
											true
										);
										?>
									</td>
									<td>
										<?php
										self::toggle_pill(
											$pick->ID,
											Pixel_Trends_CPT::META_FEATURED,
											$featured,
											__( '★ Featured', 'pixel-trends' ),
											__( 'Feature', 'pixel-trends' )
										);
										?>
									</td>
								</tr>
							<?php endforeach; ?>
						</tbody>
					</table>
				<?php else : ?>
					<div class="pixel-empty">
						<p><?php esc_html_e( 'No trending picks yet. They’ll appear here automatically after the first morning update — nothing for you to do.', 'pixel-trends' ); ?></p>
					</div>
				<?php endif; ?>
			</div>
		</div>
		<?php
	}

	/**
	 * Render a toggle as a pill button. $warn styles the "on" state red
	 * (used for Hidden, where "on" means suppressed).
	 */
	private static function toggle_pill( $post_id, $meta_key, $is_on, $on_label, $off_label, $warn = false ) {
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
			'<a class="pixel-pill %s %s" href="%s">%s</a>',
			$is_on ? 'is-on' : '',
			$warn ? 'is-warn' : '',
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
		<div class="wrap pixel-dash">
			<header class="pixel-dash-header">
				<h1><?php esc_html_e( 'Trend Settings', 'pixel-trends' ); ?></h1>
				<p class="pixel-dash-sub"><?php esc_html_e( 'One-time setup — the morning refresh handles everything else.', 'pixel-trends' ); ?></p>
			</header>

			<div class="pixel-glass pixel-dash-panel">
				<h2><?php esc_html_e( 'Shortcode', 'pixel-trends' ); ?></h2>
				<p class="pixel-panel-hint">
					<?php esc_html_e( 'Paste this anywhere you want the trending grid (homepage, landing page, sidebar). Cards link to each item’s full inner page.', 'pixel-trends' ); ?>
				</p>
				<input type="text" class="pixel-field" readonly value="[pixel_trending count=&quot;6&quot;]" onfocus="this.select();" />
				<p class="pixel-panel-hint">
					<?php esc_html_e( 'Options: count, columns, link="page|embed", show_reason="yes|no".', 'pixel-trends' ); ?>
				</p>
			</div>

			<div class="pixel-glass pixel-dash-panel">
				<h2><?php esc_html_e( 'Connection credentials', 'pixel-trends' ); ?></h2>
				<p class="pixel-panel-hint">
					<?php esc_html_e( 'These identify this site to the Pixelvolution refresh service. Regenerating them requires re-registering the site with the service.', 'pixel-trends' ); ?>
				</p>

				<label class="pixel-field-label" for="pixel-token"><?php esc_html_e( 'Site token', 'pixel-trends' ); ?></label>
				<input id="pixel-token" type="text" class="pixel-field" readonly value="<?php echo esc_attr( $token ); ?>" onfocus="this.select();" />

				<label class="pixel-field-label" for="pixel-secret"><?php esc_html_e( 'Signing secret', 'pixel-trends' ); ?></label>
				<input id="pixel-secret" type="text" class="pixel-field" readonly value="<?php echo esc_attr( $secret ); ?>" onfocus="this.select();" />

				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
					<input type="hidden" name="action" value="pixel_trends_regenerate" />
					<?php wp_nonce_field( 'pixel_trends_regenerate' ); ?>
					<button type="submit" class="pixel-button is-ghost">
						<?php esc_html_e( 'Regenerate credentials', 'pixel-trends' ); ?>
					</button>
				</form>
			</div>

			<div class="pixel-glass pixel-dash-panel">
				<h2><?php esc_html_e( 'Refresh endpoint', 'pixel-trends' ); ?></h2>
				<p class="pixel-panel-hint">
					<?php esc_html_e( 'The central service calls this URL each morning with a signed request.', 'pixel-trends' ); ?>
				</p>
				<span class="pixel-endpoint"><?php echo esc_html( rest_url( Pixel_Trends_REST::NS . '/refresh' ) ); ?></span>
			</div>
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
