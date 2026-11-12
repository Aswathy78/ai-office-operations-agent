import { describe, expect, it } from 'vitest'
import { getNavigationForRole, navigationItems } from './navigation'

describe('dashboard navigation', () => {
  it('labels the existing manager dashboard route as Task Dashboard', () => {
    const item = getNavigationForRole('manager').find((entry) => entry.id === 'dashboard')

    expect(item).toMatchObject({
      label: 'Task Dashboard',
      route: '/dashboard',
      allowedRoles: ['manager', 'hr_operations', 'admin'],
    })
    expect(navigationItems.find((entry) => entry.id === 'dashboard')).toBe(item)
  })
})
