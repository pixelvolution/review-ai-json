<?php
/**
 * Fallback single template for pixel_item inner pages.
 *
 * Themes can override this by providing their own single-pixel_item.php.
 * The inner page content itself (description, details table, menu link) is
 * built into post_content by the refresh pipeline, so this template only
 * needs to frame it.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

get_header();
?>

<main class="pixel-item-single">
	<?php
	while ( have_posts() ) :
		the_post();

		$reason = get_post_meta( get_the_ID(), Pixel_Trends_CPT::META_TREND_REASON, true );
		$is_hot = '1' === get_post_meta( get_the_ID(), Pixel_Trends_CPT::META_TRENDING, true );
		?>
		<article <?php post_class( 'pixel-item' ); ?>>
			<header class="pixel-item-header">
				<?php if ( $is_hot && $reason ) : ?>
					<span class="pixel-trending-card-badge"><?php echo esc_html( $reason ); ?></span>
				<?php endif; ?>
				<h1 class="pixel-item-title"><?php the_title(); ?></h1>
			</header>

			<?php if ( has_post_thumbnail() ) : ?>
				<div class="pixel-item-image">
					<?php the_post_thumbnail( 'large' ); ?>
				</div>
			<?php endif; ?>

			<div class="pixel-item-content">
				<?php the_content(); ?>
			</div>
		</article>
		<?php
	endwhile;
	?>
</main>

<?php
get_footer();
