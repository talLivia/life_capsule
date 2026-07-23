// Client-side JWT expiry check. This does NOT verify the signature — it's a
// UX check only (decide whether to show a login prompt / attach the token
// to a request), never a security boundary. The backend independently
// verifies the signature and expiry on every request/WS connect regardless.
export function isJwtExpired(token: string): boolean {
  try {
    const payload = token.split('.')[1]
    const decoded = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
    if (typeof decoded.exp !== 'number') return true
    return decoded.exp * 1000 <= Date.now()
  } catch {
    return true
  }
}
