// Layout only. Moving the existing controls preserves their authenticated
// handlers; changing dashboard views never writes to the flight engine.
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

export function mountDashboard(panel: HTMLDialogElement) {
  const original = [...panel.children];
  const shell = document.createElement("div");
  shell.className = "lab-shell";
  // Static application markup only. All measured values use textContent in the
  // analytics renderer, never interpolation into this template.
  shell.innerHTML = `
    <aside class="lab-sidebar">
      <div class="lab-brand"><span class="lab-brand__mark" aria-hidden="true">O</span><div>Orbital<span>Flight lab</span></div></div>
      <p class="lab-nav-label">Workspace</p>
      <nav class="lab-navigation" aria-label="Flight lab views">
        <button type="button" data-lab-view="compare" aria-pressed="true"><span aria-hidden="true">▥</span>Compare</button>
        <button type="button" data-lab-view="run" aria-pressed="false"><span aria-hidden="true">▷</span>Run flight</button>
        <button type="button" data-lab-view="data" aria-pressed="false"><span aria-hidden="true">≡</span>Recorded data</button>
      </nav>
      <div class="lab-sidebar__bottom"><span class="lab-status-dot" aria-hidden="true"></span>Local practice model<small>Recorded local flight data.</small></div>
    </aside>
    <div class="lab-workspace">
      <header class="lab-header"><div><p class="lab-eyebrow">Western Sydney Orbital</p><h2 id="lab-title">Controller comparison</h2></div><div class="lab-header__actions"><span class="lab-badge">Local estimates</span><div id="lab-close-slot"></div></div></header>
      <div class="lab-scroll">
        <section id="lab-view-compare" class="lab-view" aria-label="Controller comparison">
          <div class="lab-section-heading"><div><h3>Performance overview</h3><p>Recorded flight performance, side by side.</p></div><span id="lab-cohort-label" class="lab-badge">Matched trials</span></div>
          <div class="lab-toolbar"><div id="lab-comparison-filters" class="lab-filter-group"></div><div class="lab-toolbar__note">L2 station <span>·</span> 500 debris bodies</div></div>
          <div id="lab-publication-status"></div>
          <div class="lab-score-notice"><div><strong>STP raw score: n/a <span aria-hidden="true">·</span> Final points: n/a</strong><span>STP points unavailable. Local fuel uses a different counter.</span></div><button type="button" class="lab-text-button" data-lab-view="data">View scoring formula</button></div>
          <div id="dashboard-overview"><div class="lab-loading"><span class="lab-loading__bar"></span><h4>Loading measured flights</h4><p>The scoreboard and charts will appear here.</p></div></div>
          <details class="lab-disclosure" id="lab-custom-chart"><summary>Explore another metric</summary><div id="lab-custom-chart-content"></div></details>
          <p class="lab-footnote">Fuel and damage are local estimates. Burn and turn metrics explain controller activity; they are not extra score terms. <button type="button" class="lab-text-button" data-lab-view="data">Model notes</button></p>
        </section>
        <section id="lab-view-run" class="lab-view" aria-label="Run a controller" hidden>
          <div class="lab-section-heading"><div><h3>Put a controller in charge</h3><p>Choose the teammate, profile and flight budget.</p></div><span class="lab-badge">Seed 100 · L2</span></div>
          <div class="lab-native-status"><p id="controller-native-status" role="status">Waiting for native controller evidence.</p><p id="controller-native-evidence"></p></div>
          <div class="lab-run-grid"><section id="lab-run-controls" class="lab-surface" aria-label="Flight controls"></section><section class="lab-surface lab-run-context"><p class="lab-eyebrow">Matched test conditions</p><h4>A fresh, reproducible flight</h4><dl><div><dt>Debris bodies</dt><dd>500</dd></div><div><dt>Linear limit</dt><dd>0.5 m/s²</dd></div><div><dt>Angular limit</dt><dd>0.05 rad/s²</dd></div><div><dt>Target</dt><dd>L2 station region</dd></div></dl><p>Transit traffic, light attraction off. Run fresh replaces this tab's flight only after confirmation. Changing a controller requests Stop first.</p></section></div>
          <div class="lab-section-heading lab-recent-heading"><div><h3>Your recent flights</h3><p>Results from this connection, separate from the published comparison.</p></div></div>
          <div id="lab-room-empty" class="lab-empty"><span aria-hidden="true">⌁</span><h4>No flight recorded yet</h4><p>Select a controller above to start a fresh test. Its measured charts will appear here after the run.</p></div>
          <div id="dashboard-room-overview" hidden></div>
        </section>
        <section id="lab-view-data" class="lab-view" aria-label="Recorded data and provenance" hidden>
          <div class="lab-section-heading"><div><h3>Every number, traceable</h3><p>Exact measurements, downloads and model definitions.</p></div></div>
          <div id="lab-data-notes" class="lab-surface"></div><div id="lab-room-results"></div><div id="lab-data-content"></div>
        </section>
      </div>
    </div>`;
  const legacy = document.createElement("div");
  legacy.hidden = true;
  legacy.append(...original);
  panel.append(shell, legacy);
  panel.setAttribute("aria-labelledby", "lab-title");
  panel.removeAttribute("aria-describedby");
  byId("lab-close-slot").append(byId("close-controllers"));
  byId("close-controllers").textContent = "Close";

  const run = byId("lab-run-controls");
  const fields = byId("controller-owner").closest(".controller-fields")!;
  run.append(fields);
  for (const id of ["controller-service-status", "controller-error", "controller-fresh-warning"]) run.append(byId(id));
  run.append(byId("run-controller").closest(".controller-actions")!);
  const controllerNotes = document.createElement("details");
  controllerNotes.className = "lab-disclosure";
  const summary = document.createElement("summary"); summary.textContent = "About this profile";
  controllerNotes.append(summary, byId("controller-description"));
  run.append(controllerNotes, byId("controller-setup"));
  byId("controller-fresh-warning").textContent = "A fresh test replaces this tab's flight. You will be asked to confirm.";

  const notes = byId("lab-data-notes");
  for (const id of ["controller-score-label", "controller-score-detail", "controller-score-sources", "controller-score-comparison"]) notes.append(byId(id));
  const results = byId("lab-room-results");
  results.append(byId("controller-room-heading"), byId("controller-result-sort").closest(".controller-fields")!, byId("controller-results-table").closest(".controller-results")!);
  byId("controller-result-sort").closest<HTMLElement>(".controller-fields")!.hidden = true;
  byId("lab-data-content").append(byId("flight-analytics"));
  const analytics = byId<HTMLDetailsElement>("flight-analytics");
  analytics.open = true;
  analytics.querySelector(":scope > summary")?.remove();
  byId("analytics-read-only").textContent = "Filters and charts are read-only. Missing data stays unavailable.";

  for (const id of ["published-analytics-seed", "published-analytics-profile"]) byId("lab-comparison-filters").append(byId(id).closest("label")!);
  byId("published-analytics-seed").closest("label")!.firstChild!.textContent = "Seed ";
  byId("published-analytics-profile").closest("label")!.firstChild!.textContent = "Controller ";
  byId("lab-publication-status").append(byId("published-analytics-load-status"), byId("retry-published-analytics"));
  const custom = byId("lab-custom-chart-content");
  custom.append(byId("published-analytics-metric").closest("label")!, byId("published-analytics-plot").closest("figure")!);
  byId("published-analytics-status-detail").hidden = true;
  byId("published-analytics-heading").textContent = "Published measurements";
  byId("controller-room-heading").textContent = "Connection results";
  byId("room-analytics-heading").textContent = "Connection traces";
  for (const region of panel.querySelectorAll<HTMLElement>(".controller-results")) {
    const hint = document.createElement("p");
    hint.className = "table-scroll-hint";
    hint.textContent = "Scroll horizontally to see all columns.";
    region.prepend(hint);
  }

  const titles: Record<string, string> = { compare: "Controller comparison", run: "Run a flight", data: "Recorded data" };
  const showView = (name: string) => {
    if (!Object.hasOwn(titles, name)) return;
    for (const view of ["compare", "run", "data"]) byId(`lab-view-${view}`).hidden = view !== name;
    for (const button of shell.querySelectorAll<HTMLButtonElement>(".lab-navigation [data-lab-view]")) button.setAttribute("aria-pressed", String(button.dataset.labView === name));
    byId("lab-title").textContent = titles[name];
    shell.querySelector(".lab-scroll")!.scrollTop = 0;
  };
  for (const button of shell.querySelectorAll<HTMLButtonElement>("[data-lab-view]")) button.addEventListener("click", () => {
    showView(button.dataset.labView!);
    if (!button.closest(".lab-navigation")) shell.querySelector<HTMLButtonElement>(`.lab-navigation [data-lab-view="${button.dataset.labView}"]`)?.focus();
  });
  showView("compare");
  return { showView };
}
