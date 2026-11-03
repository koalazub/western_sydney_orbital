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
        <button type="button" data-lab-view="run" aria-pressed="true"><span aria-hidden="true">▷</span>Profiles</button>
        <button type="button" data-lab-view="compare" aria-pressed="false"><span aria-hidden="true">▥</span>Compare</button>
        <button type="button" data-lab-view="data" aria-pressed="false"><span aria-hidden="true">≡</span>Recorded data</button>
      </nav>
    </aside>
    <div class="lab-workspace">
      <header class="lab-header"><div><p class="lab-eyebrow">Western Sydney Orbital</p><h2 id="lab-title">Test profiles</h2></div><div class="lab-header__actions"><div id="lab-close-slot"></div></div></header>
      <div class="lab-scroll">
        <section id="lab-view-compare" class="lab-view" aria-label="Compare flights" hidden>
          <section id="dashboard-path-comparison" aria-labelledby="lab-title"><p>Open this panel to load saved flights.</p></section>
          <details class="lab-disclosure" id="historical-overview"><summary>Historical L2 summary</summary>
          <div class="lab-section-heading"><div><h3>L2 measurements</h3></div><span id="lab-cohort-label" class="lab-badge">Matched trials</span></div>
          <fieldset class="lab-measurement-view"><legend>Measurement view</legend><button type="button" data-measurement-view="aggregate" aria-pressed="true">Aggregate</button><button type="button" data-measurement-view="run" aria-pressed="false">Single run</button></fieldset>
          <div class="lab-toolbar"><div id="lab-comparison-filters" class="lab-filter-group"></div><div class="lab-toolbar__note">L2 practice station <span>·</span> 500 debris bodies</div></div>
          <label id="overview-run-field" class="lab-run-selector" for="overview-run" hidden>Recorded flight<select id="overview-run" aria-describedby="overview-selection-status" disabled></select></label>
          <p id="overview-selection-status" class="lab-selection-status" role="status">Aggregate summaries use all recorded seeds and repetitions.</p>
          <div id="lab-publication-status"></div>
          <div class="lab-score-notice"><div><strong>Points use the current STP formula</strong><span>Local estimates appear in Compare and connection results. Official counters are not recorded here.</span></div><button type="button" class="lab-text-button" data-lab-view="data">View scoring formula</button></div>
          <div id="dashboard-overview"><div class="lab-loading"><span class="lab-loading__bar"></span><h4>Loading measured flights</h4><p>The scoreboard and charts will appear here.</p></div></div>
          <details class="lab-disclosure" id="lab-custom-chart"><summary>Explore another metric</summary><div id="lab-custom-chart-content"></div></details>
          <p class="lab-footnote">Fuel and damage are local estimates. Burn and turn metrics explain controller activity; they are not extra score terms. <button type="button" class="lab-text-button" data-lab-view="data">Model notes</button></p>
          </details>
        </section>
        <section id="lab-view-run" class="lab-view" aria-label="Test Python profiles">
          <div class="lab-section-heading"><div><h3>Fly a teammate's code</h3><p>Connect Python running on your machine, choose a teammate and world, then Run flight. The camera follows the lander automatically. This site does not host Python.</p></div><span class="lab-badge">Local simulation</span></div>
          <section id="controller-latest-result" class="controller-latest-result" aria-labelledby="controller-latest-result-heading" hidden>
            <div><h4 id="controller-latest-result-heading">Latest result</h4><p id="controller-latest-result-profile"></p><p id="controller-latest-result-scope">Local estimate · STP formula, local measurements.</p></div>
            <div class="controller-latest-result__points"><strong id="controller-latest-result-points">n/a</strong><span>pts</span></div>
            <p id="controller-latest-result-note"></p>
            <button type="button" class="lab-text-button" data-lab-view="data">View score details</button>
            <p id="controller-latest-result-announcement" class="screen-reader-only" role="status"></p>
          </section>
          <div class="lab-run-grid"><section id="lab-run-controls" class="lab-surface" aria-label="Next profile test"></section><section class="lab-surface lab-run-context"><p class="lab-eyebrow">Selected flight</p><h4 id="controller-world-summary"></h4><p id="controller-world-description"></p><dl><div><dt>Linear limit</dt><dd>0.5 m/s²</dd></div><div><dt>Angular limit</dt><dd>0.05 rad/s²</dd></div></dl><p>The simulation runs in this tab. Keep it visible while Python flies. Stop stays below the scene beside live fuel, damage, range and speed.</p><details class="lab-disclosure"><summary>See the actual flightpath</summary><p>The orange trail shows actual positions, not a predicted route. Run flight opens the lander view. Watch flight returns to it whenever you reopen this panel.</p></details><details class="lab-disclosure"><summary>Native connection evidence</summary><div class="lab-native-status"><p id="controller-native-status" role="status">Waiting for native controller evidence.</p><p id="controller-native-evidence"></p></div><div id="lab-run-connection"></div></details></section></div>
          <div class="lab-section-heading lab-recent-heading"><div><h3>Your recent flights</h3><p>Connection results, separate from the published comparison.</p></div><button type="button" class="lab-text-button" data-lab-view="data">View receipts</button></div>
          <p id="controller-recent-data-status" class="lab-connection-data-status" role="status" aria-atomic="true">Connection data not loaded.</p>
          <div id="lab-room-empty" class="lab-empty" hidden><span aria-hidden="true">⌁</span><h4>No flight recorded yet</h4><p>Choose a profile and use Run flight. Measured charts appear here after the run.</p></div>
          <div id="dashboard-room-overview" hidden></div>
        </section>
        <section id="lab-view-data" class="lab-view" aria-label="Recorded data and provenance" hidden>
          <div class="lab-section-heading"><div><h3>Every number, traceable</h3><p>Exact measurements, downloads and model definitions.</p></div></div>
          <section class="lab-connection-data" aria-labelledby="controller-room-heading">
            <div id="lab-room-heading-slot"></div>
            <div id="lab-results-status-slot"></div>
            <div id="lab-results-actions" class="controller-actions"></div>
            <div id="lab-room-results" hidden></div>
          </section>
          <div id="lab-data-notes" class="lab-surface"></div>
          <div id="lab-data-content"></div>
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
  run.append(fields, byId("controller-profile-summary"));
  for (const id of ["controller-service-status", "controller-error", "controller-fresh-warning", "controller-run-reason"]) run.append(byId(id));
  run.append(byId("run-controller").closest(".controller-actions")!);
  byId("lab-results-actions").append(byId("refresh-controller-results"), byId("pause-controller-updates"));
  const controllerNotes = document.createElement("details");
  controllerNotes.className = "lab-disclosure";
  const summary = document.createElement("summary"); summary.textContent = "Code and adapter details";
  controllerNotes.append(summary, byId("controller-description"), byId("controller-source-metadata"));
  run.append(controllerNotes, byId("controller-setup"));
  byId("lab-run-connection").append(byId("local-python-connection"), byId("python-room"));
  byId("controller-fresh-warning").textContent = "Run flight uses the selected world and seed. It asks before replacing a flight that has already started. Changing a selection requests Stop first.";

  const notes = byId("lab-data-notes");
  for (const id of ["controller-score-label", "controller-score-detail", "controller-score-sources", "controller-score-comparison", "fuel-model-notes"]) notes.append(byId(id));
  const results = byId("lab-room-results");
  byId("lab-room-heading-slot").append(byId("controller-room-heading"));
  byId("lab-results-status-slot").append(byId("controller-results-status"));
  results.append(byId("controller-result-sort").closest(".controller-fields")!, byId("controller-results-table").closest(".controller-results")!, byId("controller-result-details"));
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

  const titles: Record<string, string> = { compare: "Compare flights", run: "Test profiles", data: "Recorded data" };
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
  showView("run");
  return { showView };
}
