"""Anything raised as CmbError is printed as a one-line error instead of a traceback."""


class CmbError(Exception):
    pass


class BenchError(CmbError):
    pass  # a run couldn't finish


class TargetError(CmbError):
    pass  # couldn't find what to launch


class ResultError(CmbError):
    pass  # missing, broken or incomparable result files
