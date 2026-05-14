import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchStats } from '../api'

export default function Home() {
  const { data: stats } = useQuery({
    queryKey: ['stats'],
    queryFn: fetchStats,
    refetchInterval: 10_000,
  })

  return (
    <div>
      <section className="hero">
        <h1>Правовой Навигатор</h1>
        <p>
          Опишите вашу ситуацию, и система найдёт relevant нормативные акты
        </p>
      </section>

      <section className="stats-section">
        <Link to="/stats" className="stat-card">
          <div className="stat-value">{stats?.total_processed ?? 0}</div>
          <div className="stat-label">Всего обработано</div>
        </Link>
        <Link to="/stats" className="stat-card">
          <div className="stat-value">{stats?.consultations ?? 0}</div>
          <div className="stat-label">Консультаций</div>
        </Link>
        <Link to="/stats" className="stat-card">
          <div className="stat-value">{stats?.searches ?? 0}</div>
          <div className="stat-label">Поисков норм</div>
        </Link>
      </section>
    </div>
  )
}
