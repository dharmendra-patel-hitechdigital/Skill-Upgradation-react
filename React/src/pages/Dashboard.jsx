import { useState } from 'react'
import { useAuth } from '../hooks/useAuth.js'
import { useApi } from '../hooks/useApi.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import { fetchStats, fetchRevenue, fetchActivity } from '../api/dashboard.api.js'
import Sidebar from '../components/layout/Sidebar.jsx'
import Topbar from '../components/layout/Topbar.jsx'
import StatCard from '../components/dashboard/StatCard.jsx'
import RevenueChart from '../components/dashboard/RevenueChart.jsx'
import RecentActivity from '../components/dashboard/RecentActivity.jsx'
import Card from '../components/ui/Card.jsx'
import Spinner from '../components/ui/Spinner.jsx'
import Button from '../components/ui/Button.jsx'

export default function Dashboard() {
  useDocumentTitle('Dashboard · Hitech')
  const { user } = useAuth()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const stats = useApi(({ signal }) => fetchStats({ signal }))
  const revenue = useApi(({ signal }) => fetchRevenue({ signal }))
  const activity = useApi(({ signal }) => fetchActivity({ signal }))

  const loading = stats.loading || revenue.loading || activity.loading
  const error = stats.error || revenue.error || activity.error

  function retryAll() {
    stats.refetch().catch(() => {})
    revenue.refetch().catch(() => {})
    activity.refetch().catch(() => {})
  }

  return (
    <div className="layout">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="layout__main">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />

        <main className="content">
          <div className="content__header">
            <div>
              <h1 className="content__title">
                Welcome back, {user?.name?.split(' ')[0] ?? 'there'} 👋
              </h1>
              <p className="content__subtitle">
                Here's what's happening with your business today.
              </p>
            </div>
            <Button variant="ghost" onClick={retryAll}>↻ Refresh</Button>
          </div>

          {error && (
            <Card className="content__error">
              <p>{error.message}</p>
              <Button variant="danger" onClick={retryAll}>Try again</Button>
            </Card>
          )}

          {loading ? (
            <div className="content__loading">
              <Spinner size={28} />
              <span>Loading your dashboard…</span>
            </div>
          ) : (
            <>
              <section className="stat-grid">
                {(stats.data?.stats ?? []).map((stat) => (
                  <StatCard key={stat.id} stat={stat} />
                ))}
              </section>

              <section className="content__grid">
                <RevenueChart series={revenue.data?.series ?? []} />
                <RecentActivity activity={activity.data?.activity ?? []} />
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  )
}
