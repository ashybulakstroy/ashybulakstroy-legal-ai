import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchStats } from '../api'

export default function StatsDetail() {
  const { data: stats } = useQuery({
    queryKey: ['stats'],
    queryFn: fetchStats,
    refetchInterval: 10_000,
  })

  return (
    <div className="page-container">
      <Link to="/" className="back-link">← На главную</Link>
      <h1 className="page-title">Статистика обработанных запросов</h1>
      <table className="stats-table">
        <thead>
          <tr>
            <th>Показатель</th>
            <th>Значение</th>
          </tr>
        </thead>
        <tbody>
          {stats?.detail.map((item) => (
            <tr key={item.key}>
              <td>{item.label}</td>
              <td><strong>{item.value}</strong></td>
            </tr>
          ))}
          {!stats && (
            <tr>
              <td colSpan={2} style={{ textAlign: 'center', color: '#94a3b8' }}>
                Загрузка...
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
