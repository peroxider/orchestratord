export default function Loading() {
  return (
    <section className="route-loading" aria-label="Loading page" aria-busy="true">
      <div className="route-loading__heading" />
      <div className="table-skeleton" aria-hidden="true">
        <i /><i /><i /><i />
      </div>
    </section>
  )
}
