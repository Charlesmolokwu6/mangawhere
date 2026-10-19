/* Where this page finds its backend (main.py). This is the one place to
   change when the backend moves to a new server or domain: every API call,
   the CORS proxy and the reader follow it. A visitor can still override it
   for themselves with ?api=... in the address.

   When main.py itself serves the page, its PUBLIC_BACKEND_URL environment
   variable takes the place of this file (see README.md). */
window.MANGAWHERE_CONFIG = {
  backend: "https://mangawhere.onrender.com"
};
