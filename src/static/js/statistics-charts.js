document.addEventListener("DOMContentLoaded", function () {
  Chart.register(ChartDataLabels);

  function translateStatus(label) {
    const labels = {
      All: "Todos",
      Completed: "Concluído",
      "In Progress": "Em andamento",
      Planning: "Planejado",
      Paused: "Pausado",
      Dropped: "Abandonado",
      Watching: "Assistindo",
      Reading: "Lendo",
      Playing: "Jogando",
      Rewatching: "Reassistindo",
      Rereading: "Relendo",
      Replaying: "Rejogando",
      "Plan to Watch": "Quero assistir",
      "Plan to Read": "Quero ler",
      "Plan to Play": "Quero jogar",
    };

    return labels[label] || label;
  }

  function translateMediaType(label) {
    const labels = {
      TV: "Séries",
      tv: "Séries",
      "TV Show": "Série",
      "TV Shows": "Séries",
      Season: "Temporada",
      Seasons: "Temporadas",
      "TV Season": "Temporada",
      "TV Seasons": "Temporadas",
      Episode: "Episódio",
      Episodes: "Episódios",
      Movie: "Filme",
      Movies: "Filmes",
      Anime: "Animes",
      Manga: "Mangás",
      Game: "Jogos",
      Games: "Jogos",
      Book: "Livros",
      Books: "Livros",
      Comic: "Quadrinhos",
      Comics: "Quadrinhos",
      Boardgame: "Jogos de tabuleiro",
      Boardgames: "Jogos de tabuleiro",
      BoardGame: "Jogos de tabuleiro",
      BoardGames: "Jogos de tabuleiro",
    };

    return labels[label] || label;
  }

  function translateChartLabel(label) {
    if (label === null || label === undefined) return label;

    const normalizedLabel = String(label);
    return translateStatus(translateMediaType(normalizedLabel));
  }

  function translatePieData(chartData) {
    return {
      ...chartData,
      labels: chartData.labels.map(translateChartLabel),
    };
  }

  function customBarTooltip(context) {
    let tooltipEl = document.getElementById("chartjs-tooltip");

    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-tooltip";
      tooltipEl.innerHTML = "<table></table>";
      document.body.appendChild(tooltipEl);
    }

    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    if (tooltipModel.body) {
      const chart = context.chart;
      const dataIndex = tooltipModel.dataPoints[0].dataIndex;
      const title = tooltipModel.title[0] || "";

      let formattedTitle = translateChartLabel(title);
      if (chart.canvas.id === "scoreStackedChart") {
        const score = parseInt(title);
        if (score === 10) {
          formattedTitle = "Nota: 10";
        } else {
          formattedTitle = `Nota: ${score},0-${score},9`;
        }
      }

      let tableBody =
        '<thead><tr><th colspan="2">' +
        formattedTitle +
        "</th></tr></thead><tbody>";
      let stackTotal = 0;

      chart.data.datasets.forEach((dataset) => {
        if (dataset.data[dataIndex] && dataset.data[dataIndex] > 0) {
          const value = dataset.data[dataIndex];
          stackTotal += value;
          const bgColor = dataset.backgroundColor;
          const label = translateChartLabel(dataset.label || "");

          tableBody +=
            "<tr>" +
            '<td style="padding-right:15px;"><span style="display:inline-block;width:12px;height:12px;background:' +
            bgColor +
            ';margin-right:8px;border-radius:2px;"></span>' +
            label +
            ":</td>" +
            '<td style="text-align:right;font-weight:bold;">' +
            value +
            "</td>" +
            "</tr>";
        }
      });

      tableBody +=
        '<tr class="total-row">' +
        "<td>Total:</td>" +
        '<td style="text-align:right;font-weight:bold;">' +
        stackTotal +
        "</td>" +
        "</tr>";

      tableBody += "</tbody>";

      const tableRoot = tooltipEl.querySelector("table");
      tableRoot.innerHTML = tableBody;
    }

    const position = context.chart.canvas.getBoundingClientRect();

    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  function customPieTooltip(context) {
    let tooltipEl = document.getElementById("chartjs-pie-tooltip");

    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-pie-tooltip";
      document.body.appendChild(tooltipEl);
    }

    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    if (tooltipModel.body) {
      const dataPoint = tooltipModel.dataPoints[0];
      const label = translateChartLabel(dataPoint.label);
      const value = dataPoint.raw;

      const dataset = context.chart.data.datasets[dataPoint.datasetIndex];
      const total = dataset.data.reduce((sum, val) => sum + val, 0);
      const percentage = Math.round((value / total) * 100);

      tooltipEl.innerHTML = `
        <div class="pie-label">${label}</div>
        <div class="pie-value">Quantidade: ${value}</div>
        <div class="pie-percent">${percentage}%</div>
      `;
    }

    const position = context.chart.canvas.getBoundingClientRect();

    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  const pieChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      datalabels: {
        color: "#D1D5DB",
        font: { size: 12 },
        formatter: (value, ctx) => {
          const total = ctx.dataset.data.reduce((acc, data) => acc + data, 0);
          const percentage = Math.round((value / total) * 100);
          const label = translateChartLabel(ctx.chart.data.labels[ctx.dataIndex]);
          return percentage > 5 ? `${label}\n${percentage}%` : "";
        },
        textAlign: "center",
        textStrokeColor: "rgba(0,0,0,0.5)",
        textStrokeWidth: 2,
        textShadowBlur: 5,
        textShadowColor: "rgba(0,0,0,0.5)",
        padding: 6,
      },
      legend: {
        position: "bottom",
        labels: {
          color: "#D1D5DB",
          padding: 20,
          usePointStyle: true,
          pointStyle: "rectRounded",
          generateLabels: function (chart) {
            const original =
              Chart.overrides.pie.plugins.legend.labels.generateLabels;
            const labels = original.call(this, chart);

            labels.forEach((label, i) => {
              label.text = `${translateChartLabel(label.text)} (${chart.data.datasets[0].data[i]})`;
              label.strokeStyle = "transparent";
            });

            return labels;
          },
        },
        margin: { top: 20 },
      },
      tooltip: {
        enabled: false,
        external: customPieTooltip,
      },
    },
    layout: { padding: { bottom: 10 } },
    elements: {
      arc: {
        borderWidth: 1,
        borderColor: "#d3d3d3",
      },
    },
  };

  const barChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      x: {
        stacked: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: {
          color: "#D1D5DB",
          callback: function (value) {
            return translateChartLabel(this.getLabelForValue(value));
          },
        },
      },
      y: {
        stacked: true,
        beginAtZero: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: { color: "#D1D5DB", precision: 0 },
      },
    },
    plugins: {
      legend: {
        position: "bottom",
        labels: {
          color: "#D1D5DB",
          padding: 20,
          boxWidth: 12,
          boxHeight: 12,
          usePointStyle: true,
          pointStyle: "rectRounded",
          textAlign: "center",
          font: {
            size: 12,
            lineHeight: 0.1,
          },
          generateLabels: function (chart) {
            const labels =
              Chart.defaults.plugins.legend.labels.generateLabels(chart);

            labels.forEach((label) => {
              label.text = translateChartLabel(label.text);
            });

            return labels;
          },
        },
      },
      tooltip: {
        enabled: false,
        mode: "index",
        external: customBarTooltip,
      },
      datalabels: {
        display: false,
      },
    },
    interaction: {
      mode: "index",
      intersect: false,
    },
  };

  function processBarData(chartData) {
    return {
      labels: chartData.labels,
      datasets: chartData.datasets
        .map((dataset) => ({
          label: translateChartLabel(dataset.label),
          data: dataset.data,
          backgroundColor: dataset.background_color,
          borderColor: "rgba(255, 255, 255, 0.1)",
          borderRadius: 6,
          borderWidth: 1,
        }))
        .filter((dataset) => dataset.data.some((value) => value > 0)),
    };
  }

  function initializeChartIfExists(elementId, chartType, data, options) {
    const element = document.getElementById(elementId);

    if (element) {
      return new Chart(element.getContext("2d"), {
        type: chartType,
        data: data,
        options: options,
      });
    }

    return null;
  }

  const mediaTypeDistributionElement = document.getElementById(
    "media_type_distribution"
  );
  if (mediaTypeDistributionElement) {
    const mediaTypeData = JSON.parse(mediaTypeDistributionElement.textContent);
    initializeChartIfExists(
      "mediaTypeChart",
      "pie",
      translatePieData(mediaTypeData),
      pieChartConfig
    );
  }

  const statusPieChartElement = document.getElementById(
    "status_pie_chart_data"
  );
  if (statusPieChartElement) {
    const statusPieData = JSON.parse(statusPieChartElement.textContent);
    initializeChartIfExists(
      "statusChart",
      "pie",
      translatePieData(statusPieData),
      pieChartConfig
    );
  }

  const statusDistributionElement = document.getElementById(
    "status_distribution"
  );
  if (statusDistributionElement) {
    const statusData = JSON.parse(statusDistributionElement.textContent);
    initializeChartIfExists(
      "statusStackedChart",
      "bar",
      processBarData(statusData),
      barChartConfig
    );
  }

  const scoreDistributionElement =
    document.getElementById("score_distribution");
  if (scoreDistributionElement) {
    const scoreData = JSON.parse(scoreDistributionElement.textContent);
    const scoreChartOptions = JSON.parse(JSON.stringify(barChartConfig));

    scoreChartOptions.scales.x.title = {
      display: true,
      text: "Nota",
      color: "#D1D5DB",
      padding: { top: 10, bottom: 0 },
    };

    scoreChartOptions.scales.y.title = {
      display: true,
      text: "Quantidade de itens",
      color: "#D1D5DB",
      padding: { top: 0, left: 10 },
    };

    scoreChartOptions.plugins.title = {
      display: true,
      text: `Nota média: ${scoreData.average_score} (${scoreData.total_scored
        } ${scoreData.total_scored === 1 ? "item" : "itens"})`,
      color: "#D1D5DB",
      padding: { bottom: 10 },
      font: { size: 14 },
    };

    scoreChartOptions.plugins.legend = barChartConfig.plugins.legend;

    scoreChartOptions.plugins.tooltip = {
      enabled: false,
      mode: "index",
      intersect: false,
      external: customBarTooltip,
    };

    initializeChartIfExists(
      "scoreStackedChart",
      "bar",
      processBarData(scoreData),
      scoreChartOptions
    );
  }
});