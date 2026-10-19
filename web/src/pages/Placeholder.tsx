interface Props {
  title: string
  milestone: string
  plans: string[]
}

export default function Placeholder({ title, milestone, plans }: Props) {
  return (
    <>
      <header className="page-header">
        <h1>{title}</h1>
        <p>預計於 {milestone} 實作</p>
      </header>
      <section className="card">
        <h2>規劃內容</h2>
        <ul>
          {plans.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      </section>
    </>
  )
}
