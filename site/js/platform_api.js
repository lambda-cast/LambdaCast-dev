/**
 * platform_api.js
 * ===============
 * Shared API client for all platform pages.
 *
 * Auth strategy (dual-mode):
 *  1. HttpOnly cookie "access_token" — set by server on login, sent automatically
 *  2. localStorage "sunalyzer_token" — fallback for browsers that block cookies
 *     on non-standard ports (e.g. localhost:8020)
 *  Both are sent on every request so either one works.
 */

const PlatformAPI = (() => {

  const BASE = "";  // same origin

  // ── Core request function ────────────────────────────────────────────
  async function request(method, path, body = null) {
    const headers = { "Content-Type": "application/json" };

    // Send token from localStorage as Authorization header (cookie is also sent)
    const stored = localStorage.getItem("sunalyzer_token");
    if (stored) {
      headers["Authorization"] = "Bearer " + stored;
    }

    const opts = { method, credentials: "include", headers };
    if (body !== null) opts.body = JSON.stringify(body);

    const res = await fetch(BASE + path, opts);

    if (res.status === 401) {
      localStorage.removeItem("sunalyzer_token");
      // Use replace so back button doesn't loop
      window.location.replace("/login");
      // Return a never-resolving promise so callers don't continue
      return new Promise(() => {});
    }

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw Object.assign(
        new Error(data.error || res.statusText),
        { status: res.status, data }
      );
    }
    return data;
  }

  // ── Auth ─────────────────────────────────────────────────────────────
  const auth = {
    login: async (username, password) => {
      const resp = await request("POST", "/api/auth/login", { username, password });
      if (resp.access_token) {
        localStorage.setItem("sunalyzer_token", resp.access_token);
      }
      return resp;
    },
    logout: async () => {
      localStorage.removeItem("sunalyzer_token");
      try { await request("POST", "/api/auth/logout"); } catch (_) {}
      window.location.href = "/login";
    },
    me: () => request("GET", "/api/auth/me"),
  };

  // ── Installations ─────────────────────────────────────────────────────
  const installations = {
    list:         ()         => request("GET",    "/api/installations"),
    get:          (id)       => request("GET",    `/api/installations/${id}`),
    create:       (data)     => request("POST",   "/api/installations", data),
    update:       (id, data) => request("PATCH",  `/api/installations/${id}`, data),
    delete:       (id)       => request("DELETE", `/api/installations/${id}`),
    location:     (id)       => request("GET",    `/api/installations/${id}/location`),
    map:          ()         => request("GET",    "/api/installations/map"),
    pvgis:        (id)       => request("GET",    `/api/installations/${id}/pvgis`),
    pvgisRefresh: (id)       => request("POST",   `/api/installations/${id}/pvgis/refresh`),
    forecastInput:(id)       => request("GET",    `/api/installations/${id}/forecast-input`),
    deviceRegistry:  ()         => request("GET",    "/api/installations/device-registry"),
    getDevice:       (id)       => request("GET",    `/api/installations/${id}/device`),
    setDevice:       (id, data) => request("POST",   `/api/installations/${id}/device`, data),
    deleteDevice:    (id)       => request("DELETE", `/api/installations/${id}/device`),
  };

  // ── Admin ─────────────────────────────────────────────────────────────
  const admin = {
    listUsers:         ()       => request("GET",    "/api/admin/users"),
    getUser:           (id)     => request("GET",    `/api/admin/users/${id}`),
    updateUser:        (id, d)  => request("PATCH",  `/api/admin/users/${id}`, d),
    disableUser:       (id)     => request("DELETE", `/api/admin/users/${id}`),
    resetPassword:     (id, p)  => request("POST",   `/api/admin/users/${id}/reset-password`, { password: p }),
    listOrganizations: ()       => request("GET",    "/api/admin/organizations"),
    createOrganization:(d)      => request("POST",   "/api/admin/organizations", d),
    listInstallations: (params) => {
      const qs = params ? "?" + new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([,v]) => v))
      ).toString() : "";
      return request("GET", "/api/admin/installations" + qs);
    },
    assignInstallation:(instId, userId) =>
      request("PATCH", `/api/admin/installations/${instId}/assign`, { user_id: userId }),
    createUser:        (d)      => request("POST",   "/api/admin/users", d),
    solarStatistics:   ()       => request("GET",    "/api/admin/solar-statistics"),
    adminMap:          (params) => {
      const qs = params ? "?" + new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([,v]) => v))
      ).toString() : "";
      return request("GET", "/api/admin/installations/map" + qs);
    },
  };

  // ── UI Utilities ──────────────────────────────────────────────────────
  function showError(msg) {
    const el = document.getElementById("alert-error");
    if (el) { el.textContent = msg; el.classList.remove("d-none"); }
    else console.error("API error:", msg);
  }

  function hideError() {
    const el = document.getElementById("alert-error");
    if (el) el.classList.add("d-none");
  }

  function showSpinner() {
    const el = document.querySelector(".spinner-overlay");
    if (el) el.classList.add("visible");
  }

  function hideSpinner() {
    const el = document.querySelector(".spinner-overlay");
    if (el) el.classList.remove("visible");
  }

  // ── Auth guards ───────────────────────────────────────────────────────

  /**
   * Ensure the current user is authenticated.
   * Redirects to /login if not. Returns the user object or null.
   */
  async function requireAuth() {
    try {
      const user = await auth.me();
      if (!user) throw new Error("no user");
      return user;
    } catch (_) {
      // Clear any stale token
      localStorage.removeItem("sunalyzer_token");
      window.location.href = "/login";
      return null;
    }
  }

  /**
   * Ensure the current user is ADMIN.
   * Redirects non-admins to /platform.
   */
  async function requireAdmin() {
    const user = await requireAuth();
    if (!user) return null;
    if (user.role !== "ADMIN") {
      window.location.href = "/platform";
      return null;
    }
    return user;
  }

  /**
   * Build the sidebar navigation HTML based on user role.
   * Call this after requireAuth() to get role-appropriate nav.
   *
   * @param {string} activePath  — current page path to highlight active link
   * @param {object} user        — user object from me()
   * @returns {string}           — HTML string of <li> elements
   */
  /**
   * Build the sidebar navigation HTML using the new sz-* design system.
   */
  function buildSidebarNav(activePath, user) {
    const isAdmin = user && user.role === "ADMIN";

    const link = (href, icon, label) => {
      const active = (activePath === href || activePath.startsWith(href + "/")) &&
                     href !== "/platform" && href !== "/admin"
                       ? " active"
                       : activePath === href ? " active" : "";
      return `<li class="sz-nav-item">
        <a class="sz-nav-link${active}" href="${href}">
          <i class="sz-nav-icon fas fa-${icon}"></i>${label}
        </a>
      </li>`;
    };

    const sep = (label) =>
      `<li class="sz-nav-section">${label}</li>`;

    if (isAdmin) {
      return `
        ${link("/admin", "gauge-high", "Tableau de bord")}
        ${sep("Parc")}
        ${link("/admin/installations", "solar-panel", "Toutes les installations")}
        ${link("/admin/map", "map-location-dot", "Carte du parc")}
        ${sep("Utilisateurs")}
        ${link("/admin/users", "users", "Utilisateurs")}
        ${sep("Système")}
        ${link("/platform/installations/new", "plus", "Ajouter une installation")}
      `;
    }

    return `
      ${link("/platform", "gauge-high", "Tableau de bord")}
      ${sep("Mon parc")}
      ${link("/platform/installations", "solar-panel", "Installations")}
      ${link("/platform/map", "map-location-dot", "Carte")}
      ${link("/platform/installations/new", "plus", "Ajouter une installation")}
    `;
  }

  /**
   * Build the top header HTML.
   * Inject into element with id="sz-header" at top of every page.
   */
  function buildHeader(user) {
    const initials = user ? user.username.slice(0,2).toUpperCase() : "??";
    const isAdmin  = user && user.role === "ADMIN";
    const homeUrl  = isAdmin ? "/admin" : "/platform";
    return `
      <a class="sz-brand" href="${homeUrl}">
        <div class="sz-brand-icon"><i class="fas fa-sun"></i></div>
        <span class="sz-brand-name">Lambda<span>Cast</span></span>
      </a>
      <div class="sz-search">
        <i class="sz-search-icon fas fa-magnifying-glass"></i>
        <input type="text" placeholder="Rechercher des installations…" id="sz-search-input" autocomplete="off"/>
      </div>
      <div class="sz-header-right">
        <button class="sz-header-btn" title="Notifications" aria-label="Notifications">
          <i class="fas fa-bell"></i>
        </button>
        ${isAdmin ? `<span class="sz-badge-admin">ADMIN</span>` : ""}
        <div class="sz-user-chip" id="sz-user-chip">
          <div class="sz-user-avatar">${initials}</div>
          <span class="sz-user-name">${user ? user.username : "…"}</span>
          <i class="fas fa-chevron-down" style="font-size:9px;color:var(--c-text-muted)"></i>
        </div>
        <button class="sz-header-btn sz-btn-ghost" id="sz-logout-btn" title="Déconnexion" aria-label="Déconnexion">
          <i class="fas fa-right-from-bracket"></i>
        </button>
      </div>
    `;
  }

  return {
    request,
    auth,
    installations,
    admin,
    showError,
    hideError,
    showSpinner,
    hideSpinner,
    requireAuth,
    requireAdmin,
    buildSidebarNav,
    buildHeader,
  };
})();
