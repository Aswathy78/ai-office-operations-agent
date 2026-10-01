export interface Meeting {
  id: string
  title: string
  startTime: string
  endTime: string
  participants: number
  status: 'Upcoming' | 'Starting soon' | 'In progress'
  joinUrl: string
}
