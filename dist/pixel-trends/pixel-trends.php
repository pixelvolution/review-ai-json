<?php
/**
 * Plugin Name:       Pixelvolution Trending Items
 * Plugin URI:        https://pixelvolution.com
 * Description:       Full item catalog with automated trending picks. Items are real posts with fully built inner pages; the [pixel_trending] shortcode surfaces the current trending items anywhere on the site. Data is refreshed by the Pixelvolution central service — no cron inside WordPress.
 * Version:           0.1.0
 * Requires at least: 5.8
 * Requires PHP:      7.4
 * Author:            Pixelvolution
 * License:           GPL-2.0-or-later
 * Text Domain:       pixel-trends
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'PIXEL_TRENDS_VERSION', '0.1.0' );
define( 'PIXEL_TRENDS_FILE', __FILE__ );
define( 'PIXEL_TRENDS_DIR', plugin_dir_path( __FILE__ ) );
define( 'PIXEL_TRENDS_URL', plugin_dir_url( __FILE__ ) );

require_once PIXEL_TRENDS_DIR . 'includes/class-pixel-trends-cpt.php';
require_once PIXEL_TRENDS_DIR . 'includes/class-pixel-trends-content-builder.php';
require_once PIXEL_TRENDS_DIR . 'includes/class-pixel-trends-rest.php';
require_once PIXEL_TRENDS_DIR . 'includes/class-pixel-trends-shortcode.php';
require_once PIXEL_TRENDS_DIR . 'includes/class-pixel-trends-admin.php';

add_action(
	'plugins_loaded',
	static function () {
		Pixel_Trends_CPT::init();
		Pixel_Trends_REST::init();
		Pixel_Trends_Shortcode::init();

		if ( is_admin() ) {
			Pixel_Trends_Admin::init();
		}
	}
);

register_activation_hook(
	__FILE__,
	static function () {
		// Generate site credentials once. The central Worker registers these
		// and uses them to authenticate every refresh call.
		if ( ! get_option( 'pixel_trends_token' ) ) {
			update_option( 'pixel_trends_token', wp_generate_password( 48, false, false ), false );
		}
		if ( ! get_option( 'pixel_trends_secret' ) ) {
			update_option( 'pixel_trends_secret', wp_generate_password( 64, false, false ), false );
		}

		Pixel_Trends_CPT::register();
		flush_rewrite_rules();
	}
);

register_deactivation_hook(
	__FILE__,
	static function () {
		flush_rewrite_rules();
	}
);
