class WorkspaceError(Exception):
    """Base exception for expected service errors."""


class WorkspaceNotFoundError(WorkspaceError):
    pass


class WorkspaceConflictError(WorkspaceError):
    pass


class InvalidRootError(WorkspaceError):
    pass


class RootChangedError(WorkspaceError):
    pass


class ScanInProgressError(WorkspaceError):
    pass


class ModelConnectionNotFoundError(WorkspaceError):
    pass


class ModelConnectionConflictError(WorkspaceError):
    pass


class CredentialStoreError(WorkspaceError):
    pass
