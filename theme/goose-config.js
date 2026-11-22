/* Remote Goose Playground configuration.
 *
 * Leave empty ("") to use automatic discovery:
 *   1. ?runner=<url> query parameter or saved browser setting
 *   2. Same-origin /api/health (local serve_playground.py or reverse proxy)
 *   3. Instant verified static snapshot (theme/goose-precomputed.js) for all book listings
 *
 * Once you deploy playground-server/ (Fly.io, Cloud Run, Render, or Docker),
 * set your HTTPS URL here—or set the GitHub Actions repository variable
 * GOOSE_PLAYGROUND_URL for automatic injection during GitHub Pages builds:
 */
window.GOOSE_PLAYGROUND_URL = window.GOOSE_PLAYGROUND_URL || "";
