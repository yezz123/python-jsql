import collections
import itertools
import logging
import re

import jinja2
import jinja2.ext
import six
from jinja2.lexer import Token

__version__ = "0.7"


class UnsafeSqlException(Exception):
    pass


NOT_DANGEROUS_RE = re.compile("^[A-Za-z0-9_]*$")


def is_safe(value):
    return NOT_DANGEROUS_RE.match(value)


class DangerouslyInjectedSql:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


def sql(engine, template, **params):
    return sql_inner(engine, template, params)


def sql_inner(engine, template, params):
    query = render(template, params)
    query, params = format_query_with_list_params(query, params)
    return SqlProxy(execute_sql(engine, query, params))


sql_inner_original = sql_inner


def render(template, params):
    params["bindparam"] = params.get("bindparam", gen_bindparam(params))
    return jenv.from_string(template).render(**params)


logger = logging.getLogger("jsql")


def assert_safe_filter(value):
    if value is None:
        return None
    if isinstance(value, DangerouslyInjectedSql):
        return value
    value = str(value)
    if not is_safe(value):
        raise UnsafeSqlException(f'unsafe sql param "{value}"')
    return value


class AssertSafeExtension(jinja2.ext.Extension):
    # based on https://github.com/pallets/jinja/issues/503
    def filter_stream(self, stream):
        for token in stream:
            if token.type == "variable_end":
                yield Token(token.lineno, "rparen", ")")
                yield Token(token.lineno, "pipe", "|")
                yield Token(token.lineno, "name", "assert_safe")
            yield token
            if token.type == "variable_begin":
                yield Token(token.lineno, "lparen", "(")


jenv = jinja2.Environment(autoescape=False, extensions=(AssertSafeExtension,))

jenv.filters["assert_safe"] = assert_safe_filter


def dangerously_inject_sql(value):
    return DangerouslyInjectedSql(value)


jenv.filters["dangerously_inject_sql"] = dangerously_inject_sql
jenv.globals["comma"] = DangerouslyInjectedSql(",")


def execute_sql(engine, query, params):
    from sqlalchemy.sql import text

    q = text(query)
    is_session = "session" in repr(engine.__class__).lower()
    return (
        engine.execute(q, params=params) if is_session else engine.execute(q, **params)
    )


BINDPARAM_PREFIX = "bp"


def gen_bindparam(params):
    keygen = key_generator()

    def bindparam(val):
        key = keygen(BINDPARAM_PREFIX)
        while key in params:
            key = keygen(BINDPARAM_PREFIX)
        params[key] = val
        return key

    return bindparam


def key_generator():
    keycnt = collections.defaultdict(itertools.count)

    def gen_key(key):
        return key + str(next(keycnt[key]))

    return gen_key


def get_param_keys(query):
    import re

    return set(re.findall("(?P<key>:[a-zA-Z_]+_list)", query))


def format_query_with_list_params(query, params):
    keys = get_param_keys(query)
    for key in keys:
        if key.endswith("_tuple_list"):
            query, params = _format_query_tuple_list_key(key, query, params)
        else:
            query, params = _format_query_list_key(key, query, params)
    return query, params


def _format_query_list_key(key, query, params):
    values = params.pop(key[1:])
    new_keys = []
    for i, value in enumerate(values):
        new_key = f"{key}_{i}"
        new_keys.append(new_key)
        params[new_key[1:]] = value
    new_keys_str = ", ".join(new_keys) or "null"
    query = query.replace(key, f"({new_keys_str})")
    return query, params


def _format_query_tuple_list_key(key, query, params):
    values = params.pop(key[1:])
    new_keys = []
    for i, value in enumerate(values):
        new_key = f"{key}_{i}"
        assert isinstance(value, tuple)
        new_keys2 = []
        for i, tuple_val in enumerate(value):
            new_key2 = f"{new_key}_{i}"
            new_keys2.append(new_key2)
            params[new_key2[1:]] = tuple_val
        new_keys.append(f'({", ".join(new_keys2)})')
    new_keys_str = ", ".join(new_keys) or "null"
    query = query.replace(key, f"({new_keys_str})")
    return query, params


class ObjProxy:
    def __init__(self, proxied):
        self._proxied = proxied

    def __iter__(self):
        return self._proxied.__iter__()

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return getattr(self, attr)
        return getattr(self._proxied, attr)


class SqlProxy(ObjProxy):
    def dicts_iter(self, dict=dict):
        result = self._proxied
        keys = result.keys()
        for r in result:
            yield dict(zip(keys, r))

    def pk_map_iter(self, dict=dict):
        result = self._proxied
        keys = result.keys()
        for r in result:
            yield (r[0], dict(zip(keys, r)))

    def kv_map_iter(self):
        result = self._proxied
        for r in result:
            yield (r[0], r[1])

    def scalars_iter(self):
        result = self._proxied
        for r in result:
            yield r[0]

    def pk_map(self, dict=dict):
        return dict(self.pk_map_iter())

    def kv_map(self, dict=dict):
        return dict(self.kv_map_iter())

    def dicts(self, dict=dict):
        return list(self.dicts_iter(dict=dict))

    def scalars(self):
        return list(self.scalars_iter())

    def scalar_set(self):
        return set(self.scalars_iter())

    def dict(self, dict=dict):
        try:
            return self.dicts(dict=dict)[0]
        except IndexError:
            return None


class SqlProxyFactory:
    """
    This class is used to create a SqlProxy object that wraps the result of a query.
    """

    def __init__(self, engine):
        self.engine = engine

    def __call__(self, query, params):
        query, params = format_query_with_list_params(query, params)
        bindparam = gen_bindparam(params)
        query = jenv.from_string(query).render(bindparam=bindparam)
        result = execute_sql(self.engine, query, params)
        return SqlProxy(result)


def get_sql_proxy_factory(engine):
    """
    This function is used to get a SqlProxyFactory object that can be used to create
    SqlProxy objects that wrap the result of a query.
    """
    return SqlProxyFactory(engine)


def get_sql_proxy(engine, query, params):
    """
    This function is used to get a SqlProxy object that wraps the result of a query.
    """
    return get_sql_proxy_factory(engine)(query, params)


def get_sql_proxy_from_template(engine, template, params):
    """
    This function is used to get a SqlProxy object that wraps the result of a query
    from a template.
    """
    query = jenv.get_template(template).render(**params)
    return get_sql_proxy(engine, query, params)
