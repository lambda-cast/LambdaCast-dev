/**
 * map_utils.js
 * ============
 * Shared Leaflet map helpers for Tunisia installation maps.
 *
 * Tunisia bounding box:
 *   SW: 30.24°N, 7.52°E
 *   NE: 37.54°N, 11.60°E
 *   Centre: ~34.0°N, 9.0°E
 */

const MapUtils = (() => {

  // Fix Leaflet's default icon path — images must be in /lib/images/ to match leaflet.min.css
  function fixLeafletIcons() {
    delete L.Icon.Default.prototype._getIconUrl;
    L.Icon.Default.mergeOptions({
      iconUrl:       '/lib/images/marker-icon.png',
      iconRetinaUrl: '/lib/images/marker-icon-2x.png',
      shadowUrl:     '/lib/images/marker-shadow.png',
    });
  }

  // Tunisia centre and zoom
  const TUNISIA_CENTER = [34.0, 9.0];
  const TUNISIA_ZOOM   = 6;
  const TUNISIA_BOUNDS = L.latLngBounds([30.24, 7.52], [37.54, 11.60]);

  // Status colours
  const STATUS_COLORS = {
    active:      '#27ae60',
    maintenance: '#f39c12',
    inactive:    '#95a5a6',
    unknown:     '#3498db',
  };

  /**
   * Create and initialise a Leaflet map centred on Tunisia.
   * @param {string} elementId  – DOM element id
   * @param {object} [opts]     – override any defaults
   */
  function createTunisiaMap(elementId, opts = {}) {
    fixLeafletIcons();

    const map = L.map(elementId, {
      center:    TUNISIA_CENTER,
      zoom:      TUNISIA_ZOOM,
      maxBounds: TUNISIA_BOUNDS.pad(0.5),
      ...opts,
    });

    // OpenStreetMap tile layer (free, no API key needed)
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map);

    return map;
  }

  /**
   * Create a single-installation mini-map (for the detail page).
   * @param {string} elementId
   * @param {number} lat
   * @param {number} lon
   * @param {string} popupHtml
   */
  function createMiniMap(elementId, lat, lon, popupHtml) {
    fixLeafletIcons();
    const map = L.map(elementId, { zoomControl: true, scrollWheelZoom: false });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(map);
    map.setView([lat, lon], 13);
    const marker = L.marker([lat, lon]).addTo(map);
    if (popupHtml) marker.bindPopup(popupHtml).openPopup();
    return map;
  }

  /**
   * Build a coloured circle marker for an installation feature.
   * @param {object} feature  GeoJSON feature
   * @param {L.LatLng} latlng
   */
  function buildMarker(feature, latlng) {
    const status = feature.properties.status || 'unknown';
    const color  = STATUS_COLORS[status] || STATUS_COLORS.unknown;
    return L.circleMarker(latlng, {
      radius:      8,
      fillColor:   color,
      color:       '#fff',
      weight:      2,
      opacity:     1,
      fillOpacity: 0.85,
    });
  }

  /**
   * Build the HTML string for a map popup.
   * @param {object} props  feature.properties
   * @param {number|null} currentProductionW  live value from telemetry (optional)
   */
  function buildPopupHtml(props, currentProductionW = null) {
    const cap    = props.installed_capacity_kwp != null
      ? `${props.installed_capacity_kwp} kWp` : '—';
    const loc    = [props.city, props.governorate].filter(Boolean).join(', ') || '—';
    const status = props.status || 'active';
    const prod   = currentProductionW != null
      ? `${Math.round(currentProductionW)} W` : '—';
    const statusDot = `<span style="display:inline-block;width:10px;height:10px;
      border-radius:50%;background:${STATUS_COLORS[status] || '#3498db'};
      margin-right:4px;vertical-align:middle;"></span>`;

    return `
      <div style="min-width:180px;font-size:13px">
        <div style="font-weight:700;font-size:14px;margin-bottom:4px">
          <a href="/platform/installations/${props.id}" style="color:#2c3e50;text-decoration:none">
            ${escHtml(props.name)}
          </a>
        </div>
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="color:#7f8c8d;padding:1px 4px 1px 0">Owner</td>
              <td>${escHtml(props.owner_username || '—')}</td></tr>
          <tr><td style="color:#7f8c8d;padding:1px 4px 1px 0">Capacity</td>
              <td><b>${escHtml(cap)}</b></td></tr>
          <tr><td style="color:#7f8c8d;padding:1px 4px 1px 0">Status</td>
              <td>${statusDot}${escHtml(status)}</td></tr>
          <tr><td style="color:#7f8c8d;padding:1px 4px 1px 0">Location</td>
              <td>${escHtml(loc)}</td></tr>
          <tr><td style="color:#7f8c8d;padding:1px 4px 1px 0">Production</td>
              <td><b>${escHtml(prod)}</b></td></tr>
        </table>
        <div style="margin-top:6px;text-align:right">
          <a href="/platform/installations/${props.id}" 
             style="font-size:12px;color:#2980b9">Details →</a>
        </div>
      </div>`;
  }

  /**
   * Add a GeoJSON FeatureCollection to a map as circle markers with popups.
   * @param {L.Map}   map
   * @param {object}  geojson              GeoJSON FeatureCollection
   * @param {object}  [liveData]           map of installation_id → currentW
   * @returns {L.GeoJSON}
   */
  function addInstallationsLayer(map, geojson, liveData = {}) {
    const layer = L.geoJSON(geojson, {
      pointToLayer: (feature, latlng) => buildMarker(feature, latlng),
      onEachFeature: (feature, layer) => {
        const currentW = liveData[feature.properties.id] ?? null;
        layer.bindPopup(buildPopupHtml(feature.properties, currentW), {
          maxWidth: 260,
        });
      },
    }).addTo(map);
    return layer;
  }

  /**
   * Fit map view to a GeoJSON layer's bounds (Tunisia fallback).
   */
  function fitToBounds(map, layer) {
    try {
      const bounds = layer.getBounds();
      if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
        return;
      }
    } catch (_) {}
    map.setView(TUNISIA_CENTER, TUNISIA_ZOOM);
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return {
    createTunisiaMap,
    createMiniMap,
    addInstallationsLayer,
    fitToBounds,
    buildPopupHtml,
    STATUS_COLORS,
    TUNISIA_CENTER,
    TUNISIA_ZOOM,
  };
})();
