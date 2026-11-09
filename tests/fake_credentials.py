from local_workspace_service.errors import CredentialStoreError


class FakeCredentialStore:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.fail_next: str | None = None
        self.operations: list[tuple[str, str]] = []

    def get(self, reference: str) -> str | None:
        self._maybe_fail("get")
        self.operations.append(("get", reference))
        return self.secrets.get(reference)

    def set(self, reference: str, secret: str) -> None:
        self._maybe_fail("set")
        self.operations.append(("set", reference))
        self.secrets[reference] = secret

    def delete(self, reference: str) -> None:
        self._maybe_fail("delete")
        self.operations.append(("delete", reference))
        self.secrets.pop(reference, None)

    def _maybe_fail(self, operation: str) -> None:
        if self.fail_next == operation:
            self.fail_next = None
            raise CredentialStoreError(f"fake credential {operation} failure")
