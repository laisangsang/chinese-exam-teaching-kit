"""Build unsafe locator samples at runtime so source releases contain no locator."""


def posix_path(*parts: str) -> str:
    return "/" + "/".join(parts)


def windows_path(*parts: str) -> str:
    return "C:" + "\\" + "\\".join(parts)


def unc_path(*parts: str) -> str:
    return "\\" * 2 + "\\".join(parts)


def file_uri(*parts: str) -> str:
    return "file:" + "//" + posix_path(*parts)
