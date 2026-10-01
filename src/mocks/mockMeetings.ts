import type { Meeting } from '../types/meeting'

export const mockMeetings: Meeting[] = [
  {
    id: 'meeting-001',
    title: 'Daily Engineering Standup',
    startTime: '10:00 AM',
    endTime: '10:30 AM',
    participants: 8,
    status: 'Upcoming',
    joinUrl: '/meetings/meeting-001',
  },
  {
    id: 'meeting-002',
    title: 'Project Review',
    startTime: '2:00 PM',
    endTime: '3:00 PM',
    participants: 6,
    status: 'Upcoming',
    joinUrl: '/meetings/meeting-002',
  },
  {
    id: 'meeting-003',
    title: 'Product Planning',
    startTime: '4:30 PM',
    endTime: '5:15 PM',
    participants: 5,
    status: 'Starting soon',
    joinUrl: '/meetings/meeting-003',
  },
]
