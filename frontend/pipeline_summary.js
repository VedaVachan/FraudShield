"use strict";
const summaryBox = document.getElementById("historical-summary");

function summaryText(tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
}

function summaryTable(headers, rows) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const heading = document.createElement("tr");
  for (const label of headers) heading.append(summaryText("th", label));
  head.append(heading);
  table.append(head);
  const body = document.createElement("tbody");
  for (const values of rows) {
    const row = document.createElement("tr");
    for (const value of values) row.append(summaryText("td", String(value)));
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  return wrap;
}

async function refreshSummary() {
  summaryBox.replaceChildren(summaryText("p", "Loading historical summary…"));
  try {
    const base = new URL(document.getElementById("api").value);
    if (!["http:", "https:"].includes(base.protocol) ||
        base.username || base.password) throw new Error("Invalid API address.");
    const response = await fetch(new URL("/pipeline/summary", base), {
      cache: "no-store", signal: AbortSignal.timeout(10000)
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const spark = data.spark;
    summaryBox.replaceChildren(
      summaryText("p", data.scope),
      summaryText("p", `Snapshot generated: ${data.generated_utc}`),
      summaryText("h3", "Historical Spark preparation"),
      summaryText("p",
        `${spark.rows.toLocaleString()} rows; ${spark.total_seconds.toFixed(2)} seconds; ` +
        `${spark.effective_rows_per_second.toFixed(1)} effective rows/second.`),
      summaryText("p", `${spark.scope}. ${spark.timing_scope}.`),
      summaryText("h3", "Original registered models")
    );
    summaryBox.append(summaryTable(
      ["Model", "Version", "Threshold", "Historical import run", "Packaging run"],
      data.registered_original_models.map(model => [
        model.name, model.version, model.threshold,
        model.historical_import_run_id, model.packaging_run_id
      ])
    ));
    summaryBox.append(
      summaryText("p", "Receipt snapshot only. These rows are not live MLflow queries. " +
        "The separately evaluated DVC baseline is a different artifact."),
      summaryText("h3", "Historical Ray Tune trials — validation only")
    );
    summaryBox.append(summaryTable(
      ["Depth", "Minimum child weight", "Average precision", "Best iteration (zero-based)"],
      data.ray_tuning.trials.map(trial => [
        trial.max_depth, trial.min_child_weight,
        trial.average_precision.toFixed(6), trial.best_iteration_zero_based
      ])
    ));
  } catch (error) {
    summaryBox.replaceChildren(summaryText(
      "p", `Historical summary unavailable: ${error.message}`
    ));
  }
}

document.getElementById("connection").addEventListener("submit", event => {
  if (!event.defaultPrevented) event.preventDefault();
  refreshSummary();
});
refreshSummary();
