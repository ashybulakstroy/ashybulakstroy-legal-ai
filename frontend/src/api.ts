import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
})

export interface StatsData {
  total_processed: number
  consultations: number
  searches: number
  detail: { label: string; value: number; key: string }[]
}

export async function fetchStats(): Promise<StatsData> {
  const { data } = await api.get<StatsData>('/stats')
  return data
}
