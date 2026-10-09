/* AURALIS dashboard charts.
 *
 * Renders on a vendored copy of Chart.js when present and degrades to the
 * server-rendered bar markup otherwise, so the dashboard never shows an empty
 * panel if the script fails to load.
 */
(function () {
  "use strict";

  var PALETTE = ["#00e5ff", "#b47cff", "#ff4ecd", "#00e5a0", "#4da3ff", "#ffb020", "#ff5470", "#7c8db5"];
  var GRID = "rgba(120,140,200,0.10)";
  var TICK = "#6b7391";

  function readJSON(id, fallback) {
    var node = document.getElementById(id);
    if (!node) return fallback;
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      console.error("Invalid JSON in #" + id, err);
      return fallback;
    }
  }

  function baseOptions(extra) {
    return Object.assign(
      {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(8,10,18,0.94)",
            borderColor: "rgba(0,229,255,0.35)",
            borderWidth: 1,
            titleColor: "#e8ecf8",
            bodyColor: "#9aa3c0",
            padding: 11,
            cornerRadius: 9,
            displayColors: true,
            boxPadding: 4,
          },
        },
        scales: {
          x: {
            grid: { color: GRID, drawTicks: false },
            border: { display: false },
            ticks: { color: TICK, font: { size: 10, family: "JetBrains Mono, monospace" }, maxRotation: 0 },
          },
          y: {
            beginAtZero: true,
            grid: { color: GRID, drawTicks: false },
            border: { display: false },
            ticks: { color: TICK, font: { size: 10 }, precision: 0, maxTicksLimit: 6 },
          },
        },
      },
      extra || {}
    );
  }

  function hexToRgba(hex, alpha) {
    var clean = String(hex).replace("#", "");
    if (clean.length === 3) {
      clean = clean[0] + clean[0] + clean[1] + clean[1] + clean[2] + clean[2];
    }
    var num = parseInt(clean, 16);
    if (isNaN(num)) return "rgba(0,229,255," + alpha + ")";
    return "rgba(" + ((num >> 16) & 255) + "," + ((num >> 8) & 255) + "," + (num & 255) + "," + alpha + ")";
  }

  /* --- line / area chart --------------------------------------------------- */

  function drawLine(canvas, labels, series) {
    if (typeof Chart === "undefined") return;
    new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: series.map(function (item) {
          return {
            label: item.label,
            data: item.data,
            borderColor: item.color,
            borderWidth: 2,
            tension: 0.36,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: item.color,
            pointHoverBorderColor: "#05060a",
            pointHoverBorderWidth: 2,
            fill: true,
            backgroundColor: function (ctx) {
              var chart = ctx.chart;
              if (!chart.chartArea) return hexToRgba(item.color, 0.12);
              var grad = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
              grad.addColorStop(0, hexToRgba(item.color, 0.3));
              grad.addColorStop(1, hexToRgba(item.color, 0));
              return grad;
            },
          };
        }),
      },
      options: baseOptions(),
    });
  }

  /* --- doughnut chart ------------------------------------------------------ */

  function drawDoughnut(canvas, labels, values, colors) {
    if (typeof Chart === "undefined") return;
    new Chart(canvas.getContext("2d"), {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [
          {
            data: values,
            backgroundColor: colors,
            borderColor: "rgba(5,6,10,0.85)",
            borderWidth: 2,
            hoverOffset: 8,
          },
        ],
      },
      options: baseOptions({
        cutout: "64%",
        scales: {},
        plugins: {
          legend: { display: false },
          tooltip: baseOptions().plugins.tooltip,
        },
      }),
    });
  }

  /* --- horizontal popularity bars ------------------------------------------ */

  function drawBars(canvas, rows) {
    if (typeof Chart === "undefined") return;
    new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: rows.map(function (row) {
          return row.title.length > 22 ? row.title.slice(0, 21) + "…" : row.title;
        }),
        datasets: [
          {
            label: "Plays",
            data: rows.map(function (row) { return row.plays; }),
            backgroundColor: function (ctx) {
              var chart = ctx.chart;
              if (!chart.chartArea) return hexToRgba(PALETTE[0], 0.7);
              var grad = chart.ctx.createLinearGradient(chart.chartArea.left, 0, chart.chartArea.right, 0);
              grad.addColorStop(0, hexToRgba(PALETTE[0], 0.85));
              grad.addColorStop(1, hexToRgba(PALETTE[1], 0.85));
              return grad;
            },
            borderRadius: 5,
            borderSkipped: false,
            barThickness: 16,
          },
        ],
      },
      options: baseOptions({
        indexAxis: "y",
        plugins: { legend: { display: false }, tooltip: baseOptions().plugins.tooltip },
        scales: {
          x: { beginAtZero: true, grid: { color: GRID }, border: { display: false }, ticks: { color: TICK, font: { size: 10 } } },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: "#9aa3c0", font: { size: 11 } } },
        },
      }),
    });
  }

  /* --- boot ---------------------------------------------------------------- */

  function init() {
    var timeseries = readJSON("chart-timeseries", []);
    if (timeseries.length) {
      var labels = timeseries.map(function (row) {
        return String(row.date).slice(5);
      });
      drawLine(
        document.getElementById("chart-plays"),
        labels,
        [{ label: "Plays", data: timeseries.map(function (r) { return r.plays; }), color: PALETTE[0] }]
      );
      if (document.getElementById("chart-listeners")) {
        drawLine(
          document.getElementById("chart-listeners"),
          labels,
          [{ label: "Unique listeners", data: timeseries.map(function (r) { return r.listeners; }), color: PALETTE[1] }]
        );
      }
    }

    var uploads = readJSON("chart-uploads", []);
    if (uploads.length && document.getElementById("chart-uploads")) {
      drawLine(
        document.getElementById("chart-uploads"),
        uploads.map(function (row) { return String(row.date).slice(5); }),
        [{ label: "Tracks uploaded", data: uploads.map(function (r) { return r.tracks; }), color: PALETTE[3] }]
      );
    }

    var distribution = readJSON("chart-distribution", []);
    if (distribution.length) {
      if (document.getElementById("chart-distribution")) {
        drawBars(document.getElementById("chart-distribution"), distribution);
      }
      if (document.getElementById("chart-genre")) {
        drawDoughnut(
          document.getElementById("chart-genre"),
          distribution.map(function (row) { return row.title; }),
          distribution.map(function (row) { return row.plays; }),
          distribution.map(function (_row, i) { return PALETTE[i % PALETTE.length]; })
        );
      }
    }

    var genres = readJSON("chart-genres", []);
    if (genres.length && document.getElementById("chart-genres")) {
      drawDoughnut(
        document.getElementById("chart-genres"),
        genres.map(function (row) { return row.genre; }),
        genres.map(function (row) { return row.plays; }),
        genres.map(function (row) { return row.color || PALETTE[0]; })
      );
    }

    var sources = readJSON("chart-sources", []);
    if (sources.length && document.getElementById("chart-sources")) {
      drawDoughnut(
        document.getElementById("chart-sources"),
        sources.map(function (row) { return row.source; }),
        sources.map(function (row) { return row.plays; }),
        sources.map(function (_row, i) { return PALETTE[i % PALETTE.length]; })
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
