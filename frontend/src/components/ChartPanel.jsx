import Plot from "react-plotly.js";

export default function ChartPanel({ figure }) {
  if (!figure) {
    return <p className="empty-hint">Henüz bir grafik yok.</p>;
  }

  return (
    <div className="chart-wrap">
      <Plot
        data={figure.data}
        layout={{ ...figure.layout, autosize: true }}
        config={{ displaylogo: false, responsive: true }}
        useResizeHandler
        style={{ width: "100%", height: "360px" }}
      />
    </div>
  );
}
