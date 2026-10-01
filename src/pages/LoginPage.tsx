import { Building2, ShieldCheck, Users } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ROLE_LABELS, type UserRole } from '../types/user'
import { useAuth } from '../contexts/AuthContext'

const demoRoles: UserRole[] = ['employee', 'manager', 'hr_operations', 'admin']

export function LoginPage() {
  const navigate = useNavigate()
  const { signInAsDemo } = useAuth()

  const handleDemoLogin = (role: UserRole) => {
    signInAsDemo(role)
    navigate('/home', { replace: true })
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <div className="brand-mark" aria-hidden="true">AO</div>
          <div>
            <strong>AI Office</strong>
            <span>Operations Agent</span>
          </div>
        </div>

        <div className="login-copy">
          <p className="eyebrow">SECURE WORKSPACE</p>
          <h1 id="login-title">Sign in to AI Office</h1>
          <p>Access meetings, tasks and AI-powered office operations from one workspace.</p>
        </div>

        <div className="login-actions">
          <button className="login-provider" type="button" onClick={() => alert('Google OAuth integration will be connected by the authentication team.')}>
            <span className="provider-icon" aria-hidden="true">G</span>
            Continue with Google
          </button>
          <button className="login-provider" type="button" onClick={() => alert('Microsoft Entra ID integration will be connected by the authentication team.')}>
            <span className="provider-icon microsoft" aria-hidden="true"><Building2 size={17} /></span>
            Continue with Microsoft
          </button>
        </div>

        <div className="login-divider"><span>Development demo</span></div>

        <div className="demo-login">
          <p className="demo-title">Choose a demo account</p>
          <p className="demo-description">This temporary selector simulates the role returned after authentication. Users will not choose their role in production.</p>
          <div className="demo-role-grid">
            {demoRoles.map((role) => (
              <button key={role} className="demo-role" type="button" onClick={() => handleDemoLogin(role)}>
                <span className="demo-role-icon" aria-hidden="true">
                  {role === 'employee' && <Users size={18} />}
                  {role === 'manager' && <Users size={18} />}
                  {role === 'hr_operations' && <Building2 size={18} />}
                  {role === 'admin' && <ShieldCheck size={18} />}
                </span>
                <span>
                  <strong>{ROLE_LABELS[role]}</strong>
                  <small>Sign in as demo</small>
                </span>
              </button>
            ))}
          </div>
        </div>

        <p className="login-note">
          Authentication is currently mocked for development. Google / Microsoft OAuth and server-side RBAC will replace this demo layer.
        </p>
      </section>
    </main>
  )
}
