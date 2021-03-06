import itertools
import operator
from collections import Iterator
import random
from . import exceptions
from . import utils

REPR_OUTPUT_SIZE = 10


def filter_values(func, values):
    """We implement a lazy filter since built-in filter() is not lazy in Python2"""
    for value in values:
        if func(value):
            yield value

class Path(object):

    class DoesNotExist(object):
        pass

    def __init__(self, path=None):
        self.path = path or []
        self._reversed = False # used for order by
        self._getters = []

    def __getattr__(self, part):
        return self.__class__(self.path + [part])

    __getitem__ = __getattr__

    def __str__(self):
        return '.'.join(self.path)

    def __eq__(self, other):
        return Query(path=self, test=operator.eq, test_args=[other])

    def __ne__(self, other):
        return Query(path=self, test=operator.ne, test_args=[other])

    def __gt__(self, other):
        return Query(path=self, test=operator.gt, test_args=[other])

    def __ge__(self, other):
        return Query(path=self, test=operator.ge, test_args=[other])

    def __lt__(self, other):
        return Query(path=self, test=operator.lt, test_args=[other])

    def __le__(self, other):
        return Query(path=self, test=operator.le, test_args=[other])

    def __invert__(self):
        """For reverse order_by"""
        return Ordering(self, reverse=True)

    def test(self, func, *args, **kwargs):
        return Query(path=self, test=func, test_args=args, test_kwargs=kwargs)

    def exists(self):
        return Query(path=self, test=check_existence, path_kwargs={'soft_fail': True})

def check_existence(v):
    return v != Path.DoesNotExist

class Ordering(object):

    def __init__(self, path, reverse=False, random=False):
        self.path = path
        self.reverse = reverse
        self.random = random

class Aggregation(object):
    def __init__(self, path, func):
        self.path = path
        self.func = func

    def __call__(self, aggregate):
        self.aggregate = aggregate

        return self

    def __repr__(self):
        return 'Aggregation({})'.format(self.func)

    def aggregate(self, data):
        return self.func(data)


class BaseQuery(object):
    def __init__(self, *args, **kwargs):
        self.inverted = kwargs.pop('inverted', False)

    def __and__(self, other):
        return QueryWrapper('AND', self, other)

    def __or__(self, other):
        return QueryWrapper('OR', self, other)

    def __invert__(self):
        return self.clone(inverted=not self.inverted)

class QueryWrapper(BaseQuery):
    def __init__(self, operator, *args, **kwargs):
        super(QueryWrapper, self).__init__(**kwargs)
        self.operator = operator
        self.subqueries = args

    def __repr__(self):
        if self.inverted:
            inverted_repr = 'NOT '
        else:
            inverted_repr = ''
        return '<QueryWrapper {0}{1} ({2})>'.format(inverted_repr, self.operator, self.operator.join([repr(self.subqueries)]))

    def clone(self, **kwargs):
        kwargs.setdefault('inverted', self.inverted)
        new_query = self.__class__(
            kwargs.get('operator', self.operator),
            *kwargs.get('subqueries', self.subqueries),
            **kwargs)
        return new_query

class Query(BaseQuery):
    """An abstract way to represent query, that will be compiled to an actual query by the manager"""
    def __init__(self, path, test, test_args=[], test_kwargs={}, path_kwargs={}, **kwargs):
        self.path_kwargs = path_kwargs
        super(Query, self).__init__(**kwargs)
        self.path = path
        self.test = test
        self.test_args = test_args
        self.test_kwargs = test_kwargs

    def __repr__(self):
        if self.inverted:
            test_repr = 'NOT {0}'.format(self.test)
        else:
            test_repr = repr(self.test)
        return '<Query {0}, {1}, {2}, {3}>'.format(self.path, test_repr, self.test_args, self.test_kwargs)

    def clone(self, **kwargs):
        kwargs.setdefault('path', self.path)
        kwargs.setdefault('test', self.test)
        kwargs.setdefault('test_args', self.test_args)
        kwargs.setdefault('test_kwargs', self.test_kwargs)
        kwargs.setdefault('inverted', self.inverted)
        kwargs.setdefault('path_kwargs', self.path_kwargs)
        return self.__class__(**kwargs)

    def test(self, func):
        return self._generate_test(
            func, ('test', self.path, func)
        )

    def exists(self):
        def impl(value):
            try:
                v = self.path.get(value)
                return True
            except exceptions.MissingAttribute:
                return False

        return QueryImpl(impl, ('exists', self.path))

    # __contains__, matches, search, any, all, exists probably

def lookup_to_path(lookup, path_class):
    path = path_class()
    for part in lookup.replace('__', '.').split('.'):
        path = getattr(path, part)
    return path

class QuerySet(object):
    def __init__(self, manager, model, query=None, orderings=None, distinct=False):
        self.model = model
        self.manager = manager
        self._populated = False
        self._data = []

        self.orderings = orderings
        self.query = query
        self.distinct_results = distinct

    def __repr__(self):
        suffix = ''
        if len(self.data) > REPR_OUTPUT_SIZE:
            suffix = " ...(remaining elements truncated)..."
        return '<QuerySet {0}{1}>'.format(self.data[:REPR_OUTPUT_SIZE], suffix)


    @property
    def data(self):
        if self._populated:
            return self._data
        return self._fetch_all()

    def _fetch_all(self):
        iterator = self.iterator()
        self._data = [item for item in iterator]
        self._populated = True
        return self._data

    def get_whole_query_kwargs(self):
        return {
            'orderings': self.orderings,
            'query': self.query,
            'distinct': self.distinct_results,
        }
    def iterator(self):
        return self.manager.execute_query(**self.get_whole_query_kwargs())

    def __eq__(self, other):
        return self.data == other

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        for value in self.data:
            yield value

    def __getitem__(self, index):
        return self.data[index]

    def _clone(self, query=None, orderings=None, **kwargs):
        orderings = orderings or self.orderings
        distinct = kwargs.get('distinct', self.distinct_results)
        return self.__class__(self.manager, model=self.model, query=query, orderings=orderings, distinct=distinct)

    def all(self):
        return self._clone()

    def first(self):
        try:
            return self.data[0]
        except IndexError:
            return None

    def last(self):
        try:
            return self.data[-1]
        except IndexError:
            return None

    def build_query(self, *args, **kwargs):
        if not args and not kwargs:
            raise ValueError('You need to provide at least a query or some keyword arguments')

        final_arg_query = None
        for arg in args:
            if not final_arg_query:
                final_arg_query = arg
                continue
            final_arg_query = final_arg_query & arg

        kwargs_query = self.build_query_from_kwargs(**kwargs)
        if kwargs_query and final_arg_query:
            final_query = final_arg_query & kwargs_query
        elif kwargs_query:
            final_query = kwargs_query
        else:
            final_query = final_arg_query

        return final_query

    def build_query_from_kwargs(self, **kwargs):
        """Convert django-s like lookup to SQLAlchemy ones"""
        query = None
        for lookup, value in kwargs.items():
            path = lookup_to_path(lookup, path_class=self.model.path_class)

            if hasattr(value, '__call__'):
                q = path.test(value)
            else:
                q = path == value

            if query:
                query = query & q
            else:
                query = q
        return query

    def _combine_query(self, query):
        if self.query:
            return query & self.query
        return query

    def filter(self, *args, **kwargs):
        final_query = self.build_query(*args, **kwargs)
        return self._clone(query=self._combine_query(final_query))

    def exclude(self, *args, **kwargs):
        final_query = ~self.build_query(*args, **kwargs)
        return self._clone(query=self._combine_query(final_query))

    def count(self):
        return len(self.data)

    def _parse_ordering(self, *paths):
        orderings = []

        for path in paths:
            if isinstance(path, Ordering):
                # probably explicit reverted ordering using ~Model.attribute
                orderings.append(path)
                continue

            reverse = False
            if isinstance(path, str):
                if path == '?':
                    orderings.append(Ordering(None, random=True))
                    continue
                reverse = path.startswith('-')
                if reverse:
                    path = path[1:]
                path = self.arg_to_path(path)
            orderings.append(Ordering(path, reverse))

        return orderings

    def get(self, *args, **kwargs):
        qs = self.filter(*args, **kwargs)
        return qs.manager.get(**qs.get_whole_query_kwargs())

    def order_by(self, *orderings):
        parsed_orderings = self._parse_ordering(*orderings)
        return self._clone(orderings=parsed_orderings)

    def arg_to_path(self, arg):
        path = arg
        if isinstance(arg, str):
            path = lookup_to_path(arg, self.model.path_class)
        return path

    def values(self, *args):
        if not args:
            raise ValueError('Empty values')

        paths = [self.arg_to_path(arg) for arg in args]
        manager_kwargs = self.get_whole_query_kwargs()

        return self.manager.values(paths, **manager_kwargs)

    def values_list(self, *args, **kwargs):
        if not args:
            raise ValueError('Empty values')

        paths = [self.arg_to_path(arg) for arg in args]

        if kwargs.get('flat', False) and len(paths) > 1:
            raise ValueError('You cannot set flat to True if you want to return multiple values')

        manager_kwargs = self.get_whole_query_kwargs()
        manager_kwargs['flat'] = kwargs.get('flat', False)
        return self.manager.values_list(paths, **manager_kwargs)

    def _build_aggregate(self, aggregation, function_name=None, key=None):
        if key:
            final_key = key
        else:
            func_name = function_name or aggregation.func.__name__ if aggregation.func.__name__ != '<lambda>' else key
            final_key = '{0}__{1}'.format(str(aggregation.path), func_name)
        values = list((aggregation.path.get(val) for val in self.data))
        return aggregation.aggregate(values), final_key

    def aggregate(self, *args, **kwargs):
        data = {}  # Isn't lazy
        flat = kwargs.pop('flat', False)

        for conf in args:
            function_name = None
            try:
                # path / function tuple aggregate
                path, func = conf
            except TypeError:
                # Django-like aggregate
                path = lookup_to_path(conf.attr_name, path_class=self.model.path_class)
                func = conf.aggregate
                function_name = conf.name

            aggregation = Aggregation(path, func)
            aggregate, key = self._build_aggregate(aggregation, function_name=function_name)
            data[key] = aggregate
        for key, conf in kwargs.items():
            function_name = None
            try:
                # path / function tuple aggregate
                path, func = conf
            except TypeError:
                # Django-like aggregate
                path = lookup_to_path(conf.attr_name, self.model.path_class)
                func = conf.aggregate
                function_name = conf.name

            aggregation = Aggregation(path, func)
            aggregate, key = self._build_aggregate(aggregation, function_name=function_name, key=key)
            data[key] = aggregate


        if flat:
            return list(data.values())  # Isn't lazy

        return data

    def distinct(self):
        return self._clone(distinct=True)

    def exists(self):
        return len(self) > 0
