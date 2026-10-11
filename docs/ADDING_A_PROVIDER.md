# Adding a new provider

Everything above the provider layer (bot menus, pricing, billing, jobs, dashboard) works on generic rows (`Provider`, `Location`, `Plan`, `OSImage`, `Server`). A new datacenter needs **one class and one line of registration**.

## 1. Implement `CloudProvider`

Create `app/providers/<name>.py` and subclass `app.providers.base.CloudProvider`:

```python
from app.providers.base import CloudProvider, CreatedServer, IPInfo, OutOfStock, ProviderError, ...

class ExampleProvider(CloudProvider):
    kind = "example"              # stored in providers.kind
    display_name = "Example Cloud"

    def __init__(self, api_token, options=None):
        super().__init__(api_token, options)
        self._client = httpx.AsyncClient(base_url="https://api.example.com", headers={...})

    async def list_locations(self): ...       # -> [ProviderLocation(code, name, country, city, currency, ipv4_hourly, ipv4_monthly)]
    async def list_plans(self): ...           # -> [ProviderPlan(..., prices={loc: (hourly, monthly)}, available_in={loc,...})]
    async def list_images(self): ...          # -> [ProviderImage(code, name, flavor, arch="x86,arm")]
    async def create_server(self, *, name, plan_code, location_code, image_code, labels): ...
    async def get_server(self, server_id): ...  # None if it does not exist
    async def find_servers(self, labels): ...   # used for idempotent retries and status sync
    async def delete_server(self, server_id): ...  # must not fail if already gone
    async def power_on / power_off / shutdown / reboot(self, server_id): ...
    async def rebuild(self, server_id, image_code): ...      # -> new root password
    async def change_primary_ipv4(self, server_id): ...      # -> IPInfo of the new IP
    async def create_reserve_ip(self, *, location_code, name): ...
    async def swap_primary_ipv4(self, server_id, new_ip_id): ...  # -> IPInfo of the old IP (kept)
    async def delete_ip(self, ip_id): ...
    async def close(self): await self._client.aclose()
```

Rules the rest of the system relies on:

| Rule | Why |
|---|---|
| Methods return only **after the operation finished** (poll async actions yourself). | The worker reports success to the user right after the call returns. |
| Raise `ProviderError(msg, code=..., retryable=True/False)` for API errors. | `retryable=True` makes the job runner retry with backoff; `False` fails the job right away. |
| Raise `OutOfStock` when a plan can't be ordered in that location. | The order is refunded and the plan is hidden until the next sync. |
| Labels passed to `create_server` must be stored, and `find_servers(labels)` must find them. | A retried `create_server` job finds the half-created server instead of creating a duplicate. Status sync lists `managed-by=hetzbot`. |
| `ServerInfo.status` is normalised: `running`, `off`, `starting`, `stopping`, `rebuilding`, `migrating`, `unknown`. | Shown to users as online/offline. |
| `ServerInfo.locked=True` only for provider-side locks (abuse, billing). | Triggers the abuse workflow. |
| `ServerInfo.datacenter` / `IPInfo.datacenter` = the placement unit an IP can move within. | Reserve-IP swaps are offered only when these match. |
| Prices are **net, in the provider's currency** (EUR or USD). | Converted with the matching exchange rate. Add a rate setting if you need another currency. |

If the provider has no concept of reserve or primary IPs, raise `ProviderError("not supported")` in those methods. The bot shows the user a generic failure, so also hide those locations from IP creation if needed.

## 2. Register it

`app/providers/registry.py`:

```python
from app.providers.example import ExampleProvider

PROVIDER_CLASSES = {
    HetznerProvider.kind: HetznerProvider,
    ExampleProvider.kind: ExampleProvider,
    FakeProvider.kind: FakeProvider,
}
```

## 3. Add it in the dashboard

**Providers → Add provider**: code `example-main`, kind `example`, a display name and the API token (stored encrypted). Press **Sync catalog**, then review **Plans & prices** and **Locations & OS**. Once a second active provider has plans, the bot automatically shows the provider selection step.

## 4. Test it

Copy `tests/test_hetzner.py` and drive your class with `httpx.MockTransport`. Check the request bodies, error mapping and the order of calls for the IP change. Also run the flow tests with your provider swapped in for `fake` if you can build an in-memory double.
