// A small line of the core's 15-minute closes, drawn as plain SVG. A missing
// bucket (null) breaks the line rather than being bridged.

export function sparkPath(values: Array<number | null>, width = 100, height = 30): string {
  const present = values.filter((v): v is number => v !== null);
  if (present.length < 2) return "";
  const low = Math.min(...present);
  const high = Math.max(...present);
  const span = high - low || 1;
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  let path = "";
  let drawing = false;
  values.forEach((value, index) => {
    if (value === null) {
      drawing = false;
      return;
    }
    const x = (index * step).toFixed(2);
    const y = (height - 1 - ((value - low) / span) * (height - 2)).toFixed(2);
    path += `${drawing ? "L" : "M"}${x} ${y} `;
    drawing = true;
  });
  return path.trim();
}

export function Sparkline({ values, tone, label }: { values: Array<number | null>; tone: "up" | "down" | "flat" | "unknown" | "muted"; label: string }) {
  const path = sparkPath(values);
  if (!path) return <span className="sparkline sparkline-empty" aria-hidden="true" />;
  return <svg className={`sparkline tone-${tone}`} viewBox="0 0 100 30" preserveAspectRatio="none" role="img" aria-label={label}>
    <path d={path} fill="none" vectorEffect="non-scaling-stroke" />
  </svg>;
}
