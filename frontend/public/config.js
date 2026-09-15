/**
 * Where the API lives. Empty means "the origin this page came from", which is the
 * normal case: the backend serves these files itself, so the page, the REST routes and
 * the Socket.IO endpoint are all on one origin and no CORS is involved.
 *
 * Point it elsewhere to run the frontend against a backend on another host, and set
 * DETECTOR_CORS_ORIGINS on that backend to allow this page through. A ?api=... query
 * parameter overrides whatever is set here, which is handy for a one-off.
 */
window.DETECTOR_API = '';
