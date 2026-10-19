export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    throw new ApiError(res.status, await res.text())
  }
  return res.json() as Promise<T>
}

export interface Health {
  status: string
  version: string
  schema_version: number
  providers: string[]
}
