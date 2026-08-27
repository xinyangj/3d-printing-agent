import { useEffect, useState } from 'react'
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
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.error?.message ?? response.statusText)
  }
  return response.json() as Promise<T>
}

type SetupStatus = {
  status:
    | 'connect_not_ready'
    | 'device_not_bound'
    | 'confirmation_required'
    | 'confirmed'
    | 'stale'
  message: string
  active: boolean
  profile_id: string
  device_ref: string | null
  device_name: string | null
  readiness: {
    installed: boolean
    scheme_registered: boolean
    signature_valid: boolean
    ready: boolean
    message: string
    installation_digest: string | null
    signer_thumbprint: string | null
    file_version: string | null
  }
  confirmation: {
    installation_digest: string
    signer_thumbprint: string
    file_version: string
    confirmed_by: string
    confirmed_at: string
  } | null
}

export function BambuConnectSetupCard({
  profileId,
  profileRevision,
  localManagement,
}: {
  profileId: string
  profileRevision: number | null
  localManagement: boolean
}) {
  const queryClient = useQueryClient()
  const [acknowledged, setAcknowledged] = useState(false)
  const status = useQuery({
    queryKey: ['bambu-connect-setup', profileId],
    queryFn: () =>
      request<SetupStatus>(
        `/slicing-profiles/${encodeURIComponent(
          profileId,
        )}/bambu-connect-status${
          profileRevision ? `?profile_revision=${profileRevision}` : ''
        }`,
      ),
    enabled: Boolean(profileId && profileRevision),
  })
  const refresh = () =>
    queryClient.invalidateQueries({
      queryKey: ['bambu-connect-setup', profileId],
    })
  const install = useMutation({
    mutationFn: () => request('/bambu-connect/install', { method: 'POST' }),
    onSuccess: refresh,
  })
  const open = useMutation({
    mutationFn: () => request('/bambu-connect/open', { method: 'POST' }),
    onSuccess: refresh,
  })
  const confirm = useMutation({
    mutationFn: () => {
      if (
        !status.data?.device_ref ||
        !status.data.readiness.installation_digest
      ) {
        throw new Error('Refresh Connect and H2D setup before confirming')
      }
      return request(
        `/slicing-profiles/${encodeURIComponent(
          profileId,
        )}/bambu-connect-confirmation`,
        {
          method: 'POST',
          body: JSON.stringify({
            expected_device_ref: status.data.device_ref,
            expected_profile_revision: profileRevision,
            expected_installation_digest:
              status.data.readiness.installation_digest,
            acknowledged: true,
            confirmed_by: 'local-web',
          }),
        },
      )
    },
    onSuccess: async () => {
      setAcknowledged(false)
      await refresh()
    },
  })
  const revoke = useMutation({
    mutationFn: () =>
      request(
        `/slicing-profiles/${encodeURIComponent(
          profileId,
        )}/bambu-connect-confirmation${
          profileRevision ? `?profile_revision=${profileRevision}` : ''
        }`,
        { method: 'DELETE' },
      ),
    onSuccess: refresh,
  })

  useEffect(() => {
    setAcknowledged(false)
  }, [profileId, status.data?.status])

  const error =
    status.error ?? install.error ?? open.error ?? confirm.error ?? revoke.error
  const pending =
    install.isPending || open.isPending || confirm.isPending || revoke.isPending

  return (
    <section className="inspection-panel connect-setup-card">
      <span className="section-label">Official Bambu Connect handoff</span>
      <h2>Bambu Connect setup</h2>
      <p>
        Connect owns visible sign-in, printer selection, upload, and the final
        Print/Send confirmation. This app never reads its private account
        storage.
      </p>

      {status.isLoading ? (
        <small>Checking signed Connect installation…</small>
      ) : status.data ? (
        <>
          <div className="connect-setup-step">
            <span>1</span>
            <div>
              <strong>
                {status.data.readiness.ready
                  ? `Signed Connect ${status.data.readiness.file_version ?? ''} ready`
                  : status.data.readiness.message}
              </strong>
              <small>
                {status.data.readiness.signature_valid
                  ? 'Official executable signature verified'
                  : 'Trusted signature required'}
              </small>
            </div>
            {!status.data.readiness.ready && localManagement && (
              <button
                className="secondary-action"
                disabled={pending}
                onClick={() => install.mutate()}
              >
                {install.isPending ? 'Installing…' : 'Install official Connect'}
              </button>
            )}
          </div>

          <div className="connect-setup-step">
            <span>2</span>
            <div>
              <strong>Sign in visibly inside Bambu Connect</strong>
              <small>
                Confirm that {status.data.device_name ?? 'the bound H2D'} appears
                in Connect.
              </small>
            </div>
            <button
              className="secondary-action"
              disabled={
                !localManagement ||
                !status.data.readiness.installed ||
                !status.data.readiness.signature_valid ||
                pending
              }
              onClick={() => open.mutate()}
            >
              Open Bambu Connect
            </button>
          </div>

          <div className="connect-setup-step">
            <span>3</span>
            <div>
              <strong>{status.data.message}</strong>
              {status.data.confirmation && (
                <small>
                  User-confirmed{' '}
                  {new Date(
                    status.data.confirmation.confirmed_at,
                  ).toLocaleString()}
                </small>
              )}
            </div>
          </div>

          {!status.data.active && status.data.readiness.ready && status.data.device_ref && (
            <>
              <label className="checkbox-label">
                <input
                  checked={acknowledged}
                  disabled={!localManagement || pending}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  type="checkbox"
                />
                I signed in to Bambu Connect and can see{' '}
                {status.data.device_name ?? 'the bound H2D'}.
              </label>
              <button
                className="primary-action"
                disabled={!acknowledged || !localManagement || pending}
                onClick={() => confirm.mutate()}
              >
                Confirm Connect setup
              </button>
            </>
          )}

          {status.data.active && (
            <button
              className="text-button"
              disabled={!localManagement || pending}
              onClick={() => revoke.mutate()}
            >
              Reconfirm Connect setup
            </button>
          )}

          {!localManagement && (
            <p className="part-warning">
              Connect desktop setup is available only from the local web UI.
            </p>
          )}
        </>
      ) : null}

      {error && <p className="error-copy">{error.message}</p>}
    </section>
  )
}
