export function getRouterBasename(pathname: string): string | undefined {
  if (/^\/copaw(?:\/|$)/.test(pathname)) return "/copaw"
  if (/^\/console(?:\/|$)/.test(pathname)) return "/console"
  return undefined
}

export function getLoginPath(pathname = window.location.pathname): string {
  const basename = getRouterBasename(pathname)
  return `${basename || ""}/login`
}

export function isLoginPath(pathname = window.location.pathname): boolean {
  return pathname === getLoginPath(pathname)
}

export function redirectToLogin(): void {
  window.location.href = getLoginPath()
}
