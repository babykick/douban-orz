# -*- coding:utf8 -*-
import sys
from collections import defaultdict

from .sql_executor import SqlExecutor
from .base_mgr import OrzField, OrzPrimaryField
from .configs import CacheConfigMgr, Config

ONE_HOUR=3600

HEADQUARTER_VERSION = 'orz-hq-ver:1'

SINGLE_OBJ_CACHE_KEY_PATTERN = "%s|{table_name}|single_obj_ck|ver:{ver}|id:" % HEADQUARTER_VERSION

KV_TO_IDS_CACHE_KEY_PATTERN = "%s|{table_name}|kv_to_ids|ver:{ver}|" % HEADQUARTER_VERSION


def make_orders(fields):
    mapper = {
        OrzField.KeyType.DESC: lambda x, y: x + [("-%s" % y.field_name,)],
        OrzField.KeyType.ASC: lambda x, y: x + [("%s" % y.field_name,)],
        OrzField.KeyType.AD: lambda x, y: x + [("%s" % y.field_name, ), ("-%s" % y.field_name)],
        OrzField.KeyType.NOT_INDEX: lambda x, y: x,
        OrzField.KeyType.ONLY_INDEX: lambda x, y: x,
    }
    return tuple(reduce(lambda x, y:mapper[y.as_key](x, y), fields, []))


class CachedOrmManager(object):
    def __init__(self, table_name, cls, db_fields, sqlstore, mc,
                 cache_ver='', order_combs=tuple()):
        self.single_obj_ck = SINGLE_OBJ_CACHE_KEY_PATTERN.format(table_name=table_name, ver=cache_ver)
        self.cls = cls
        self.mc = mc
        self.db_field_names = [i.field_name for i in db_fields]
        self.primary_field = (i for i in db_fields if isinstance(i, OrzPrimaryField)).next()
        self.sql_executor = SqlExecutor(table_name, self.primary_field.field_name,  [f.field_name for f in db_fields], sqlstore)
        kv_to_ids_ck = KV_TO_IDS_CACHE_KEY_PATTERN.format(table_name=table_name, ver=cache_ver)
        self.config_mgr = CacheConfigMgr()

        orders = make_orders(db_fields) + order_combs
        self.config_mgr.generate_basic_configs(kv_to_ids_ck,
                                               [f.field_name for f in db_fields if f.as_key], orders)

        self.default_vals = dict((k.field_name, k.default) for k in db_fields if k.default != OrzField.NO_DEFAULT)


    def _get_and_refresh(self, sql_executor, primary_field_vals, force_flush=False):
        res = []
        if not force_flush:
            di = dict(zip(primary_field_vals, self.mc.get_list([self.single_obj_ck + str(i) for i in primary_field_vals])))
        else:
            di = {}

        for i in primary_field_vals:
            if di.get(i) is not None:
                obj = di[i]
            else:
                obj = self.cls(**sql_executor.get(i))
                self.mc.set(self.single_obj_ck + str(i), obj, ONE_HOUR)
            res.append(obj)
        return res

    def get(self, id=None, force_flush=False, **kw):
        ret = self.gets_by(id=id, force_flush=force_flush)
        if len(ret) == 0:
            return None
        return ret[0]

    def get_multiple_ids(self, primary_field_vals):
        return self._get_and_refresh(self.sql_executor, primary_field_vals)

    def _amount_check(self, amount, start_limit):
        if not start_limit:
            return True

        start, limit = start_limit
        if start + limit > amount:
            return True

        return False


    def fetch(self, force_flush, conditions, order_keys = None, start_limit = None):
        amount = sys.maxint
        sql_executor = self.sql_executor
        if conditions:
            config = self.config_mgr.lookup_gets_by(conditions.keys(), order_keys)
            if config is None or (amount is not None and self._amount_check(amount, start_limit)):
                primary_field_vals = self.sql_executor.get_ids(conditions, start_limit, order_keys)
                return [self.cls(**self.sql_executor.get(i)) for i in primary_field_vals]

            _start_limit = (0, amount) if amount is not None else tuple()

            ck = config.to_string(conditions)

            if not force_flush:
                primary_field_vals = self.mc.get(ck)
            else:
                primary_field_vals = None

            if primary_field_vals is None:
                primary_field_vals = sql_executor.get_ids(conditions, _start_limit, order_keys)
                self.mc.set(ck, primary_field_vals, ONE_HOUR)

        else:
            primary_field_vals = sql_executor.get_ids(conditions, start_limit, order_keys)

        if start_limit:
            start, limit = start_limit
            primary_field_vals = primary_field_vals[start:start + limit]

        return self._get_and_refresh(sql_executor, primary_field_vals, force_flush)

    def create(self, raw_kwargs, transactional=False):
        return self.cls(**self.create_record(raw_kwargs, transactional))

    def create_record(self, raw_kwargs, transactional=False):
        kwargs = []
        kwargs = dict((k, (v() if callable(v) else v)) for k, v in self.default_vals.iteritems())
        kwargs.update(raw_kwargs)

        cks = self._get_cks(kwargs, self.db_field_names)
        self.mc.delete_multi(cks)

        sql_data = dict((field, kwargs.pop(field)) for field in self.db_field_names if field in kwargs)
        _primary_field_val = self.sql_executor.create(sql_data, transactional)

        return self.sql_executor.get(_primary_field_val)

    def _get_cks(self, data_src, fields):
        cks = []
        configs = {}
        for field in fields:
            for i in self.config_mgr.lookup_related(field):
                configs[i.as_key()]=i

        for c in configs.itervalues():
            field_cks = c.to_string(data_src)
            cks.append(field_cks)
        return cks

    def save(self, ins, transactional=False):
        cks = []

        if not ins.dirty_fields:
            return 0

        datum = dict((f, getattr(ins, "hidden____org_" + f)) for f in self.db_field_names)
        cks.extend(self._get_cks(datum, ins.dirty_fields))
        cks.extend(self._get_cks(ins, ins.dirty_fields))

        all_cks = cks + [self.single_obj_ck+str(ins.id)]
        self.mc.delete_multi(all_cks)

        sql_data = dict((field, getattr(ins, field)) for field in ins.dirty_fields)
        ret = self.sql_executor.update_row(ins.id, sql_data, transactional)

        data = self.sql_executor.get(ins.id)
        for i in self.db_field_names:
            setattr(ins, i, data[i])

        ins.dirty_fields = set()
        return ret


    def delete(self, ins, transactional=False):
        cks = self._get_cks(ins, [self.primary_field.name]+self.db_field_names)

        self.mc.delete_multi(cks + [self.single_obj_ck+str(ins.id)])

        return self.sql_executor.delete(ins.id, transactional)

    def gets_by(self, order_by=None, start=0, limit=sys.maxint, force_flush=False, **kw):
        if order_by is None:
            real_order_by = self.primary_field.as_default_order_key()
        else:
            real_order_by = (order_by, ) if type(order_by) is not tuple else order_by
        return self.fetch(force_flush, kw, real_order_by, (start, limit))

    def count_by(self, **conditions):
        config = self.config_mgr.lookup_normal(conditions.keys())
        if config is None:
            return self.sql_executor.calc_count(conditions)

        ck = config.to_string(conditions)
        c = self.mc.get(ck)
        if c is None:
            ret = self.sql_executor.calc_count(conditions)
            self.mc.set(ck, ret, ONE_HOUR)
            return ret
        else:
            return c
