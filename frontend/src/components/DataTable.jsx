function formatValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return value.toLocaleString("tr-TR", { maximumFractionDigits: 2 });
  }
  return String(value);
}

function formatPeriod(value) {
  if (!value) return "—";
  return String(value).slice(0, 7); // YYYY-MM
}

export default function DataTable({ table }) {
  if (!table || !table.columns || table.columns.length === 0) {
    return <p className="empty-hint">Henüz bir tablo oluşturulmadı.</p>;
  }

  const { columns, units, rows, all_columns: allColumns } = table;
  // The session's table keeps every column the conversation has built; this
  // turn shows the ones its own question is about. Saying so is the
  // difference between "a column went missing" and "it is still there".
  const hidden = (allColumns || []).filter((col) => !columns.includes(col));

  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Dönem</th>
            {columns.map((col) => (
              <th key={col}>
                {col}
                {units?.[col] ? <span className="unit-tag">{units[col]}</span> : null}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={row.period ?? index}>
              <td className="period-cell">{formatPeriod(row.period)}</td>
              {columns.map((col) => (
                <td key={col}>{formatValue(row[col])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hidden.length > 0 ? (
        <p className="table-note">
          Bu soruyla ilgili {columns.length} sütun gösteriliyor; oturumdaki {hidden.length} sütun
          ({hidden.join(", ")}) saklı — "tabloya ekle" diyerek geri getirebilirsiniz.
        </p>
      ) : null}
    </div>
  );
}
