interface StatCardProps {
  label: string;
  value: string;
  detail?: string;
}

export function StatCard({ label, value, detail }: StatCardProps) {
  return (
    <article className="stat-card">
      <p className="meta-label">{label}</p>
      <strong>{value}</strong>
      {detail ? <p className="muted">{detail}</p> : null}
    </article>
  );
}
