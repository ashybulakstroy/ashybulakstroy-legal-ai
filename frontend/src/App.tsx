import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Home from './pages/Home'
import StatsDetail from './pages/StatsDetail'
import './index.css'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/stats" element={<StatsDetail />} />
      </Routes>
    </BrowserRouter>
  )
}
