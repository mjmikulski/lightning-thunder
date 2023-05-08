# NOTE This delays annotation evaluation, allowing a class to annotate itself
#   This feature is available in Python 3.7 and later.
#   This import (like all __future__ imports) must be at the beginning of the file.
from __future__ import annotations

import sys
import collections.abc
from numbers import Number
from typing import Any, Sequence, Callable, Type, Union, Optional, Tuple, List
from types import MappingProxyType
import re

#
# Common utils importable by any other file
#
# TODO: make all of these callable through utils (the idea is that this can be imported even if utils cannot)

# Python 3.10 introduces a new dataclass parameter, `slots`, which we'd like to use
# by default. However, we still want to support Python 3.9, so we need to
# conditionally set the default dataclass parameters.
# MappingProxyType is used to make the configuration immutable.
if sys.version_info >= (3, 10):
    default_dataclass_params = MappingProxyType({"frozen": True, "slots": True})
else:
    default_dataclass_params = MappingProxyType({"frozen": True})

#
# Functions and classes (and metaclasses) related to class management
#


# Use this a metaclass to get a singleton pattern
#
# Ex.
# class SingletonClass(BaseClasses..., metaclass=Singleton):
#   ...
#
# When lazy initialization is not required, an alternative to a singleton class
#   is just using a new file and importing it.
class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


#
# Interfaces to support type annotation and instance checks without
#   creating circular dependencies.
#
class ProxyInterface:
    @property
    def name(self):
        pass

    def type_string(self):
        pass


class NumberProxyInterface:
    pass


class TensorProxyInterface:
    pass


class SymbolInterface:
    name: str
    is_prim: bool
    id: Optional[Any]


class BoundSymbolInterface:
    sym: SymbolInterface
    args: Sequence
    kwargs: dict
    output: Any
    subsymbols: Sequence[BoundSymbolInterface]


#
# Functions related to error handling
#


def check(cond: bool, s: Callable[[], str], exception_type: Type[Exception] = RuntimeError) -> None:
    """Helper function for raising an error_type (default: RuntimeError) if a boolean condition fails.

    s is a callable producing a string to avoid string construction if the error check is passed.
    """
    if not cond:
        raise exception_type(s())


def check_type(x: Any, types: Union[Type, Sequence[Type]]):
    check(
        isinstance(x, types),
        lambda: f"{x} had an unexpected type {type(x)}. Supported types are {types}",
        exception_type=ValueError,
    )


#
# Functions related to Python object queries and manipulation
#


# TODO Review these imports -- not a big deal since we depend on both
#   But if we want to be extensible in the future we probably need to use langctx_for here
#   Which means that this function needs to move to codeutils (probably fine) or
#   this needs to take a dependency on langctx.py (probably not great)
import torch
import numpy as np


# A somewhat hacky way to check if an object is a collection but not a string or
#   tensor object
def is_collection(x: Any) -> bool:
    return isinstance(x, collections.abc.Collection) and not isinstance(x, (str, torch.Tensor, np.ndarray))


def sequencify(x: Any) -> Sequence:
    # NOTE: strings in Python are sequences, which requires this special handling
    #   to avoid "hello" being treated as "h", "e", "l", "l", "o"
    if not isinstance(x, Sequence) or isinstance(x, str):
        return (x,)
    else:
        return x


def get_module(name: str) -> Any:
    return sys.modules[name]


#
# Functions related to common checks
#


def check_valid_length(length: int):
    """Validates that an object represents a valid dimension length."""

    check_type(length, int)
    check(length >= 0, lambda: f"Found invalid length {length}!")


def check_valid_shape(shape: Union[Tuple[int, ...], List[int]]):
    """Validates that a sequence represents a valid shape."""

    check_type(shape, (Tuple, List))

    for l in shape:
        check_valid_length(l)


#
# Functions related to printing code
#

tab = "  "


def indent(level):
    return f"{tab * level}"


_type_to_str_map = {
    bool: "bool",
    int: "int",
    float: "float",
    complex: "complex",
    str: "str",
}


def is_printable_type(typ: Type) -> bool:
    return typ in _type_to_str_map


# TODO Document this function and ensure it's used consistently
# TODO Add more basic Python types
def print_type(typ: Type) -> str:
    # Special cases basic Python types

    if typ in _type_to_str_map:
        return _type_to_str_map[typ]

    # Handles the general case of where types are printed like
    #   <class 'float'>
    #   Does this by capturing the name in quotes with a regex
    s = str(typ)
    result = re.search(".+'(.+)'.*", s)
    return f"'{result.group(1)}'"


#
# Functions related to constructing callables from Python strings
#


def compile_and_exec(fn_name: str, python_str: str, program_name: str, ctx: dict) -> Callable:
    try:
        code = compile(python_str, program_name, mode="exec")
        exec(code, ctx)
    except Exception as e:
        print("Encountered an exception while trying to compile the following program:")
        print(python_str)
        raise e

    return ctx[fn_name]
