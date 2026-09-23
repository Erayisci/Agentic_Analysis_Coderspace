import Plot from "react-plotly.js";

export default function ChartPanel({ figure }) {
  if (!figure) {
    return (
      <p className="empty-hint">
        Henüz bir grafik yok. Sorunuzda <strong>"grafik"</strong>, <strong>"çiz"</strong> veya{" "}
        <strong>"görselleştir"</strong> gibi bir kelime geçirirseniz grafik otomatik çizilir —
        örneğin: "Konut kredisi ve faiz oranını grafik olarak göster".
      </p>
    );
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
