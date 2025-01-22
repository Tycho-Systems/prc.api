from typing import (
    Optional,
    List,
    TYPE_CHECKING,
    Callable,
    Type,
    TypeVar,
    Dict,
    Union,
    Literal,
)
from .utility import KeylessCache, Cache, CacheConfig, Requests, InsensitiveEnum
from .utility.exceptions import *
from .models import *
import asyncio
import httpx

if TYPE_CHECKING:
    from .client import PRC

R = TypeVar("R")


class ServerCache:
    """Server long-term object caches and config. TTL in seconds, 0 to disable. (max_size, TTL)"""

    def __init__(
        self,
        players: CacheConfig = (50, 0),
        vehicles: CacheConfig = (50, 1 * 60 * 60),
        join_logs: CacheConfig = (150, 6 * 60 * 60),
    ):
        self.players = Cache[int, ServerPlayer](*players)
        self.vehicles = KeylessCache[Vehicle](*vehicles)
        self.join_logs = KeylessCache[JoinEntry](
            *join_logs, sort=(lambda e: e.created_at, True)
        )


def _refresh_server(func):
    async def wrapper(self: "Server", *args, **kwargs):
        server = self._server if isinstance(self, ServerModule) else self
        result = await func(self, *args, **kwargs)
        self._global_cache.servers.set(server._id, server)
        return result

    return wrapper


def _ephemeral(func):
    async def wrapper(self: "Server", *args, **kwargs):
        cache_key = f"{func.__name__}_cache"
        if hasattr(self, cache_key):
            cached_result, timestamp = getattr(self, cache_key)
            if (asyncio.get_event_loop().time() - timestamp) < self._ephemeral_ttl:
                return cached_result

        result = await func(self, *args, **kwargs)
        setattr(self, cache_key, (result, asyncio.get_event_loop().time()))
        return result

    return wrapper


class Server:
    """The main class to interface with PRC ER:LC server APIs. `ephemeral_ttl` is how long, in seconds, results are cached for."""

    def __init__(
        self,
        client: "PRC",
        server_key: str,
        ephemeral_ttl: int = 5,
        cache: ServerCache = ServerCache(),
        requests: Optional[Requests] = None,
        ignore_global_key: bool = False,
    ):
        self._client = client

        client._validate_server_key(server_key)
        self._id = client._get_server_id(server_key)

        self._global_cache = client._global_cache
        self._server_cache = cache
        self._ephemeral_ttl = ephemeral_ttl

        global_key = client._global_key
        headers = {"Server-Key": server_key}
        if global_key and not ignore_global_key:
            headers["Authorization"] = global_key
        self._requests = requests or Requests(
            base_url=client._base_url + "/server", headers=headers
        )
        self._ignore_global_key = ignore_global_key

        self.logs = ServerLogs(self)
        self.commands = ServerCommands(self)

    name: Optional[str] = None
    owner: Optional[ServerOwner] = None
    co_owners: List[ServerOwner] = []
    player_count: Optional[int] = None
    max_players: Optional[int] = None
    join_key: Optional[str] = None
    account_requirement = None
    team_balance: Optional[bool] = None

    def _get_player(self, id: Optional[int] = None, name: Optional[str] = None):
        for _, player in self._server_cache.players.items():
            if id and player.id == id:
                return player
            if name and player.name == name:
                return player

    async def _safe_close(self):
        await self._requests._close()

    def _handle_error_code(self, error_code: Optional[int] = None):
        if error_code is None:
            raise PRCException("An unknown error has occured.")

        errors: List[Callable[..., APIException]] = [
            UnknownError,
            CommunicationError,
            InternalError,
            MissingServerKey,
            InvalidServerKeyFormat,
            InvalidServerKey,
            InvalidGlobalKey,
            BannedServerKey,
            InvalidCommand,
            ServerOffline,
            RateLimit,
            RestrictedCommand,
            ProhibitedMessage,
            RestrictedResource,
            OutOfDateModule,
        ]

        for error in errors:
            error = error()
            if error_code == error.error_code:
                invalid_key = None
                if isinstance(error, InvalidGlobalKey):
                    invalid_key = self._requests._default_headers.get("Authorization")
                elif isinstance(error, (InvalidServerKey, BannedServerKey)):
                    invalid_key = self._requests._default_headers.get("Server-Key")

                if invalid_key:
                    self._requests._invalid_keys.add(invalid_key)

                raise error

        raise APIException(error_code, "An unknown API error has occured.")

    def _handle(self, response: httpx.Response, return_type: Type[R]) -> R:
        if not response.is_success:
            self._handle_error_code(response.json().get("code"))
        return response.json()

    @_refresh_server
    @_ephemeral
    async def get_status(self):
        """Get the current server status."""
        return ServerStatus(
            self, data=self._handle(await self._requests.get("/"), Dict)
        )

    @_refresh_server
    @_ephemeral
    async def get_players(self):
        """Get all online server players."""
        return [
            ServerPlayer(self, data=p)
            for p in self._handle(await self._requests.get("/players"), List[Dict])
        ]

    @_ephemeral
    async def get_queue(self):
        """Get all players in the server join queue."""
        return [
            QueuedPlayer(self, id=p)
            for p in self._handle(await self._requests.get("/queue"), List[int])
        ]

    @_refresh_server
    @_ephemeral
    async def get_bans(self):
        """Get all server bans."""
        return [
            Player(self._client, data=p)
            for p in (self._handle(await self._requests.get("/bans"), Dict)).items()
        ]

    @_refresh_server
    @_ephemeral
    async def get_vehicles(self):
        """Get all spawned vehicles in the server."""
        return [
            self._server_cache.vehicles.add(Vehicle(self, data=v))
            for v in self._handle(await self._requests.get("/vehicles"), List[Dict])
        ]


class ServerModule:
    """A class implemented by modules used by the main `Server` class to interface with specific PRC ER:LC server APIs."""

    def __init__(self, server: Server):
        self._server = server

        self._global_cache = server._global_cache
        self._server_cache = server._server_cache
        self._ephemeral_ttl = server._ephemeral_ttl

        self._requests = server._requests
        self._handle = server._handle


class ServerLogs(ServerModule):
    """Interact with PRC ER:LC server logs APIs."""

    def __init__(self, server: Server):
        super().__init__(server)

    @_refresh_server
    @_ephemeral
    async def get_joins(self):
        """Get server join logs."""
        [
            JoinEntry(self._server, data=e)
            for e in self._handle(await self._requests.get("/joinlogs"), List[Dict])
        ]
        return self._server_cache.join_logs.items()

    @_refresh_server
    @_ephemeral
    async def get_kills(self):
        """Get server kill logs."""
        return [
            KillEntry(self._server, data=e)
            for e in self._handle(await self._requests.get("/killlogs"), List[Dict])
        ]

    @_refresh_server
    @_ephemeral
    async def get_commands(self):
        """Get server command logs."""
        return [
            CommandEntry(self._server, data=e)
            for e in self._handle(await self._requests.get("/commandlogs"), List[Dict])
        ]

    @_refresh_server
    @_ephemeral
    async def get_mod_calls(self):
        """Get server mod call logs."""
        return [
            ModCallEntry(self._server, data=e)
            for e in self._handle(await self._requests.get("/modcalls"), List[Dict])
        ]


CommandTargetPlayerNameWithAll = Literal["all"]
CommandTargetPlayerWithAll = Union[CommandTargetPlayerNameWithAll, int]
CommandTargetPlayer = Union[str, int]


class ServerCommands(ServerModule):
    """Interact with the PRC ER:LC server remote command execution API."""

    def __init__(self, server: Server):
        super().__init__(server)

    async def _raw(self, command: str):
        """Run a raw string command as a remote player in the server."""
        self._handle(
            await self._requests.post("/command", json={"command": command.strip()}),
            Dict,
        )

    async def run(
        self,
        name: CommandName,
        targets: Optional[List[CommandTargetPlayer]] = None,
        args: Optional[List[CommandArg]] = None,
        text: Optional[str] = None,
    ):
        """Run any command as a remote player in the server."""
        command = f":{name} "

        if targets:
            command += ",".join([str(t) for t in targets]) + " "

        if args:
            command += (
                " ".join(
                    [
                        (a.value if isinstance(a, InsensitiveEnum) else str(a))
                        for a in args
                    ]
                )
                + " "
            )

        if text:
            command += text

        await self._raw(command)

    async def kill(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Kill players in the server."""
        await self.run("kill", targets=targets)

    async def heal(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Heal players in the server."""
        await self.run("heal", targets=targets)

    async def wanted(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Make players wanted in the server."""
        await self.run("wanted", targets=targets)

    async def unwanted(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Remove wanted status from players in the server."""
        await self.run("unwanted", targets=targets)

    async def jail(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Jail players in the server."""
        await self.run("jail", targets=targets)

    async def unjail(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Unjail players in the server."""
        await self.run("unjail", targets=targets)

    async def refresh(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Refresh players in the server."""
        await self.run("refresh", targets=targets)

    async def respawn(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Respawn players in the server."""
        await self.run("load", targets=targets)

    async def teleport(self, targets: List[CommandTargetPlayerNameWithAll], to: str):
        """Teleport players to another player in the server."""
        await self.run("tp", targets=targets, args=[to])

    async def kick(self, targets: List[CommandTargetPlayerNameWithAll]):
        """Kick players from the server."""
        await self.run("kick", targets=targets)

    async def ban(self, targets: List[CommandTargetPlayerWithAll]):
        """Ban players from the server."""
        await self.run("ban", targets=targets)

    async def unban(self, targets: List[CommandTargetPlayer]):
        """Unban players from the server."""
        await self.run("unban", targets=targets)

    async def mod(self, targets: List[CommandTargetPlayerWithAll]):
        """Grant moderator permissions to players in the server."""
        await self.run("mod", targets=targets)

    async def unmod(self, targets: List[CommandTargetPlayerWithAll]):
        """Revoke moderator permissions from players in the server."""
        await self.run("unmod", targets=targets)

    async def admin(self, targets: List[CommandTargetPlayerWithAll]):
        """Grant admin permissions to players in the server."""
        await self.run("admin", targets=targets)

    async def unadmin(self, targets: List[CommandTargetPlayerWithAll]):
        """Revoke admin permissions from players in the server."""
        await self.run("unadmin", targets=targets)

    async def hint(self, text: str):
        """Send a temporary hint (banner) undismissable message to the server."""
        await self.run("h", text=text)

    async def announce(self, text: str):
        """Send an announcement (popup) dismissable message to the server."""
        await self.run("m", text=text)

    async def pm(self, targets: List[CommandTargetPlayerNameWithAll], text: str):
        """Send a private (popup) dismissable message to players in the server."""
        await self.run("pm", targets=targets, text=text)

    async def set_priority(self, seconds: int = 0):
        """Set the server priority timer. Shows an undismissable countdown notification to all players until it reaches `0`. Leave empty or set to `0` to disable."""
        await self.run("prty", args=[seconds])

    async def set_peace(self, seconds: int = 0):
        """Set the server peace timer. Shows an undismissable countdown notification to all players until it reaches `0` while disabling PVP damage. Leave empty or set to `0` to disable."""
        await self.run("pt", args=[seconds])

    async def set_time(self, hour: int):
        """Set the server current time of day as the given hour. Uses 24-hour formatting (`12` = noon, `0`/`24` = midnight)."""
        await self.run("time", args=[hour])

    async def set_weather(self, type: Weather):
        """Set the weather in the server. `Weather.SNOW` can only be set during winter."""
        await self.run("weather", args=[type])

    async def start_fire(self, type: FireType):
        """Start a fire at a random location in the server."""
        await self.run("startfire", args=[type])

    async def stop_fires(self):
        """Stop all fires in the server."""
        await self.run("stopfire")
