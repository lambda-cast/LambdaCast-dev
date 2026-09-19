/**
 * shell.js
 * Injects the application shell (header + sidebar) into the page.
 * Call initShell(user, activePath) after auth.
 */

function initShell(user, activePath) {
  // ── Header ──────────────────────────────────────────────────────
  const hdr = document.getElementById("sz-header");
  if (hdr) {
    hdr.innerHTML = PlatformAPI.buildHeader(user);
  }

  // ── Sidebar ─────────────────────────────────────────────────────
  const sb = document.getElementById("sz-sidebar-nav");
  if (sb) {
    sb.innerHTML = PlatformAPI.buildSidebarNav(activePath, user);
  }

  // ── Wire ALL logout buttons (header + sidebar footer) ───────────
  // Handles: #sz-logout-btn (header), #sz-logout-footer-btn (sidebar),
  //          legacy #logout-btn if still present
  document.querySelectorAll(
    "#sz-logout-btn, #sz-logout-footer-btn, #logout-btn, .sz-logout-trigger"
  ).forEach(el => {
    el.addEventListener("click", () => PlatformAPI.auth.logout());
  });

  // ── Search (global — filters .sz-searchable rows) ───────────────
  const searchInput = document.getElementById("sz-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      const q = e.target.value.toLowerCase();
      document.querySelectorAll(".sz-searchable tr[data-search]").forEach(row => {
        const txt = (row.dataset.search || "").toLowerCase();
        row.style.display = txt.includes(q) ? "" : "none";
      });
    });
  }
}

/**
 * Standard page init: auth check + shell injection + hideSpinner.
 * Usage:
 *   const user = await pageInit('/admin');
 *   if (!user) return;
 *   // your page code
 */
async function pageInit(activePath, requireAdmin = false) {
  PlatformAPI.showSpinner();
  try {
    const user = requireAdmin
      ? await PlatformAPI.requireAdmin()
      : await PlatformAPI.requireAuth();
    if (!user) return null;
    initShell(user, activePath);
    return user;
  } catch (err) {
    PlatformAPI.showError("Erreur de chargement : " + (err.message || "Erreur inconnue"));
    return null;
  } finally {
    PlatformAPI.hideSpinner();
  }
}

// Override showError/showSuccess for sz- classes
const _origShowError = PlatformAPI.showError;
PlatformAPI.showError = function(msg) {
  const el = document.getElementById("alert-error");
  if (el) {
    el.className = "sz-alert sz-alert-err";
    el.innerHTML = `<i class="fas fa-circle-exclamation"></i><span>${msg}</span>`;
    el.style.display = "";
  } else { _origShowError(msg); }
};

PlatformAPI.showSuccess = function(msg) {
  const el = document.getElementById("alert-success");
  if (el) {
    el.className = "sz-alert sz-alert-ok";
    el.innerHTML = `<i class="fas fa-circle-check"></i><span>${msg}</span>`;
    el.style.display = "";
    setTimeout(() => { el.style.display = "none"; }, 4000);
  }
};
