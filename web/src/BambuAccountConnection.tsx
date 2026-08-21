import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const API = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.error?.message ?? response.statusText)
  }
  return response.json()
}

export type CredentialStatus = {
  configured: boolean
  region: 'global' | 'china' | null
  protection: string
}

export type CloudDevice = {
  device_id: string
  device_ref: string
  name: string
  model: string
  online: boolean
}

type StudioSessionStatus = {
  installed: boolean
  running: boolean
  session_present: boolean
  signed_in: boolean
  region: 'global' | 'china' | null
  account_hint: string | null
  session_updated_at: string | null
  error_code: string | null
  message: string
}

type CredentialImportResult = {
  credential: CredentialStatus
  devices: CloudDevice[]
}

export function BambuAccountConnection({
  localCredentialSetup,
  credentialStatus,
  devices,
}: {
  localCredentialSetup: boolean
  credentialStatus: CredentialStatus | undefined
  devices: CloudDevice[] | undefined
}) {
  const queryClient = useQueryClient()
  const [riskAccepted, setRiskAccepted] = useState(false)
  const [fallbackRegion, setFallbackRegion] = useState<'global' | 'china'>('global')
  const [fallbackToken, setFallbackToken] = useState('')
  const studioSession = useQuery({
    queryKey: ['bambu-studio-session'],
    queryFn: () => request<StudioSessionStatus>('/bambu-studio-session'),
    enabled: localCredentialSetup,
    retry: false,
  })

  const refreshConnectionQueries = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cloud-credential-status'] }),
      queryClient.invalidateQueries({ queryKey: ['cloud-devices'] }),
    ])
  }

  const openStudio = useMutation({
    mutationFn: () =>
      request<StudioSessionStatus>('/bambu-studio-session/open', { method: 'POST' }),
    onSuccess: (status) => {
      queryClient.setQueryData(['bambu-studio-session'], status)
    },
  })
  const importStudioSession = useMutation({
    mutationFn: () =>
      request<CredentialImportResult>('/cloud-credentials/import-bambu-studio', {
        method: 'POST',
        body: JSON.stringify({ experimental_acknowledged: riskAccepted }),
      }),
    onSuccess: refreshConnectionQueries,
  })
  const saveFallbackToken = useMutation({
    mutationFn: () =>
      request<CredentialImportResult>('/cloud-credentials', {
        method: 'POST',
        body: JSON.stringify({
          access_token: fallbackToken,
          region: fallbackRegion,
          experimental_acknowledged: riskAccepted,
        }),
      }),
    onSuccess: async () => {
      setFallbackToken('')
      await refreshConnectionQueries()
    },
  })
  const disconnect = useMutation({
    mutationFn: () => request<CredentialStatus>('/cloud-credentials', { method: 'DELETE' }),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ['cloud-devices'] })
      await queryClient.invalidateQueries({ queryKey: ['cloud-credential-status'] })
    },
  })

  const connected = credentialStatus?.configured === true
  const deviceCount = devices?.length
  const credentialMutationPending =
    importStudioSession.isPending || saveFallbackToken.isPending || disconnect.isPending
  const error =
    studioSession.error ??
    openStudio.error ??
    importStudioSession.error ??
    saveFallbackToken.error ??
    disconnect.error

  return (
    <section className="inspection-panel account-connection-panel">
      <span className="section-label">Experimental Bambu Cloud inventory</span>
      <div className="account-connection-summary">
        <strong className={connected ? 'ready' : 'not-ready'}>
          {connected
            ? `Bambu account connected · ${credentialStatus.region}`
            : 'Bambu account connection required'}
        </strong>
        {connected && (
          <small>
            Access is protected with Windows DPAPI. Bambu Studio remains the owner of the
            sign-in session.
          </small>
        )}
      </div>
      <p>
        Sign in through official Bambu Studio, then explicitly import its local session. This app
        never receives your password or SMS code and uses the unsupported private API only for
        read-only printer and AMS inventory.
      </p>

      {connected && deviceCount === 0 && (
        <p className="part-warning">
          Account connected, but Bambu Cloud returned no bound printers. Bind the H2D to this same
          account in Bambu Studio or Bambu Handy, then refresh the device list.
        </p>
      )}
      {connected && typeof deviceCount === 'number' && deviceCount > 0 && (
        <p className="account-device-count">
          {deviceCount} bound {deviceCount === 1 ? 'printer' : 'printers'} detected.
        </p>
      )}

      {localCredentialSetup ? (
        <>
          <ol className="account-connection-steps">
            <li className="account-connection-step">
              <span className="account-step-number">1</span>
              <div>
                <strong>Open official Bambu Studio</strong>
                <p>
                  {studioSession.data?.installed
                    ? studioSession.data.running
                      ? 'Bambu Studio is running.'
                      : 'Bambu Studio is installed and ready to open.'
                    : studioSession.isPending
                      ? 'Checking the local Bambu Studio installation…'
                      : 'Bambu Studio is not installed or configured on this server.'}
                </p>
                <button
                  className="secondary-action"
                  disabled={!studioSession.data?.installed || openStudio.isPending}
                  onClick={() => openStudio.mutate()}
                  type="button"
                >
                  {openStudio.isPending ? 'Opening…' : 'Open Bambu Studio'}
                </button>
              </div>
            </li>
            <li className="account-connection-step">
              <span className="account-step-number">2</span>
              <div>
                <strong>Sign in with Bambu</strong>
                <p>
                  Complete region selection and phone/SMS or email sign-in in the Studio window.
                  Then return here and check the local session.
                </p>
                <div className="settings-actions">
                  <button
                    className="secondary-action"
                    disabled={studioSession.isFetching}
                    onClick={() => void studioSession.refetch()}
                    type="button"
                  >
                    {studioSession.isFetching ? 'Checking…' : 'Check sign-in status'}
                  </button>
                  {studioSession.data?.signed_in && (
                    <span className="account-session-ready">
                      Signed in
                      {studioSession.data.account_hint
                        ? ` as ${studioSession.data.account_hint}`
                        : ''}
                      {studioSession.data.region ? ` · ${studioSession.data.region}` : ''}
                    </span>
                  )}
                </div>
                {!studioSession.data?.signed_in && studioSession.data?.message && (
                  <small>{studioSession.data.message}</small>
                )}
              </div>
            </li>
            <li className="account-connection-step">
              <span className="account-step-number">3</span>
              <div>
                <strong>{connected ? 'Refresh this app’s connection' : 'Import signed-in session'}</strong>
                <p>
                  The access token is validated first, then stored with Windows DPAPI. It is never
                  returned to this browser or written to a plaintext file.
                </p>
                <label className="checkbox-label">
                  <input
                    checked={riskAccepted}
                    onChange={(event) => setRiskAccepted(event.target.checked)}
                    type="checkbox"
                  />
                  I understand the read-only private cloud API is unofficial and may change or
                  revoke access.
                </label>
                <div className="settings-actions">
                  <button
                    className="primary-action"
                    disabled={
                      !studioSession.data?.signed_in ||
                      !riskAccepted ||
                      credentialMutationPending
                    }
                    onClick={() => importStudioSession.mutate()}
                    type="button"
                  >
                    {importStudioSession.isPending
                      ? 'Validating and importing…'
                      : connected
                        ? 'Refresh from Bambu Studio'
                        : 'Import signed-in session'}
                  </button>
                  {connected && (
                    <button
                      className="danger-action"
                      disabled={credentialMutationPending}
                      onClick={() => disconnect.mutate()}
                      type="button"
                    >
                      {disconnect.isPending ? 'Disconnecting…' : 'Disconnect this app'}
                    </button>
                  )}
                </div>
                {connected && (
                  <small>Disconnecting this app does not sign you out of Bambu Studio.</small>
                )}
              </div>
            </li>
          </ol>

          <details className="advanced-account-fallback">
            <summary>Advanced fallback: import an existing access token</summary>
            <p>
              Use this only when the installed Studio session format is unsupported. The token is
              validated and DPAPI-protected using the same security boundary.
            </p>
            <label>
              Region
              <select
                value={fallbackRegion}
                onChange={(event) =>
                  setFallbackRegion(event.target.value as 'global' | 'china')
                }
              >
                <option value="global">Global</option>
                <option value="china">China</option>
              </select>
            </label>
            <label>
              Existing Bambu access token
              <input
                autoComplete="off"
                type="password"
                value={fallbackToken}
                onChange={(event) => setFallbackToken(event.target.value)}
              />
            </label>
            <button
              className="secondary-action"
              disabled={!fallbackToken.trim() || !riskAccepted || credentialMutationPending}
              onClick={() => saveFallbackToken.mutate()}
              type="button"
            >
              {saveFallbackToken.isPending ? 'Validating…' : 'Validate fallback token'}
            </button>
          </details>
        </>
      ) : (
        <p className="part-warning">
          Account connection is available only on the server at http://127.0.0.1:8000.
        </p>
      )}
      {error && <p className="error-copy">{error.message}</p>}
    </section>
  )
}
