# -*- coding:utf8 -*-
import sys
from collections import defaultdict

from .sql_executor import SqlExecutor
from .base_mgr import OrmItem, OrzField, OrzPrimaryField
from .configs import CacheConfigMgr, Config

ONE_HOUR=3600

HEADQUARTER_VERSION = 'a3'


def make_orders(fields):
    mapper = {
        OrzField.KeyType.DESC: lambda x, y: x + [("-%s" % y.name,)],
        OrzField.KeyType.ASC: lambda x, y: x + [("%s" % y.name,)],
        OrzField.KeyType.AD: lambda x, y: x + [("%s" % y.name, ), ("-%s" % y.name)],
        OrzField.KeyType.NOT_INDEX: lambda x, y: x,
    }
    return tuple(reduce(lambda x, y:mapper[y.as_key](x, y), fields, []))

class CachedOrmManager(object):
    # TODO mgr.db_fields is sql_executor's
    def __init__(self, table_name, cls, primary_field, db_fields, sqlstore, mc,
                 cache_ver='', extra_orders=tuple()):
        self.single_obj_ck = HEADQUARTER_VERSION + "%s:single_obj_ck:" % table_name + cache_ver
        self.sql_executor = SqlExecutor(table_name, [f.name for f in db_fields], sqlstore)
        self.cls = cls
        self.mc = mc
        self.primary_field = primary_field
        kv_to_ids_ck = HEADQUARTER_VERSION + "%s:kv_to_ids:" % table_name + cache_ver
        self.config_mgr = CacheConfigMgr()

        orders = make_orders(db_fields) + extra_orders
        self.config_mgr.generate_basic_configs(kv_to_ids_ck,
                                               [f.name for f in db_fields if f.as_key], orders)

        self.default_vals = dict((k.name, k.default) for k in db_fields if k.default != OrzField.NO_DEFAULT)

    def __getattr__(self, field):
        return getattr(self.sql_executor, field)

    def _get_and_refresh(self, sql_executor, ids, force_flush=False):
        res = []
        if not force_flush:
            di = dict(zip(ids, self.mc.get_list([self.single_obj_ck + str(i) for i in ids])))
        else:
            di = {}

        for i in ids:
            if di.get(i) is not None:
                obj = di[i]
            else:
                obj = self.cls(**sql_executor.get(i))
                self.mc.set(self.single_obj_ck + str(i), obj, ONE_HOUR)
            res.append(obj)
        return res

    def get(self, id, force_flush=False):
        ret = self.gets_by(id=id, force_flush=force_flush)
        if len(ret) == 0:
            return None
        return ret[0]

    def get_multiple_ids(self, ids):
        return self._get_and_refresh(self.sql_executor, ids)

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
            if amount is not None and \
                self._amount_check(amount, start_limit):
                ids = self.sql_executor.get_ids(conditions, _tart_limit, order_keys)
                return [self.cls(**self.sql_executor.get(i)) for i in ids]

            _start_limit = (0, amount) if amount is not None else tuple()

            ck = config.to_string(conditions)

            if not force_flush:
                ids = self.mc.get(ck)
            else:
                ids = None

            if ids is not None:
                ret = self._get_and_refresh(self.sql_executor, ids)
            else:
                ids = self.sql_executor.get_ids(conditions, _start_limit, order_keys)
                self.mc.set(ck, ids, ONE_HOUR)
                ret = self._get_and_refresh(self.sql_executor, ids, force_flush)

        else:
            ids = self.sql_executor.get_ids(conditions, start_limit, order_keys)
            ret = [self.cls(**self.sql_executor.get(i)) for i in ids]

        if start_limit:
            start, limit = start_limit
            return ret[start:start + limit]
        return ret

    def create(self, raw_kwargs):
        kwargs = self.default_vals.copy()
        kwargs.update(raw_kwargs)
        cks = self._get_cks(kwargs, self.db_fields)
        self.mc.delete_multi(cks)

        sql_data = dict((field, kwargs.pop(field)) for field in self.db_fields if field in kwargs)
        _primary_field_val = self.sql_executor.create(sql_data)

        sql_data[self.primary_field.name] = _primary_field_val

        return self.cls(**sql_data)

    def _get_cks(self, data_src, fields):
        cks = []
        for field in fields:
            configs = self.config_mgr.lookup_related(field)
            for c in configs:
                field_cks = c.to_string(data_src)
                cks.append(field_cks)
        return cks

    def save(self, ins):
        cks = []
        datum = dict((f, getattr(ins, "hidden____org_" + f)) for f in self.db_fields)
        cks.extend(self._get_cks(datum, ins.dirty_fields))
        cks.extend(self._get_cks(ins, ins.dirty_fields))

        all_cks = cks + [self.single_obj_ck+str(ins.id)]
        self.mc.delete_multi(all_cks)

        sql_data = dict((field, getattr(ins, field)) for field in ins.dirty_fields)
        self.sql_executor.update_row(ins.id, sql_data)

    def delete(self, ins):
        cks = self._get_cks(ins, [self.primary_field.name]+self.db_fields)
        self.mc.delete_multi(cks + [self.single_obj_ck+str(ins.id)])

        self.sql_executor.delete(ins.id)

    def gets_by(self, order_by=None, start=0, limit=sys.maxint, force_flush=False, **kw):
        if order_by is None:
            real_order_by = self.primary_field.as_default_order_key()
        else:
            real_order_by = (order_by, ) if type(order_by) is not tuple else order_by
        return self.fetch(force_flush, kw, real_order_by, (start, limit))

    def count_by(self, **conditions):
        config = self.config_mgr.lookup_normal(conditions.keys())
        ck = config.to_string(conditions)
        c = self.mc.get(ck)
        if c is None:
            ret = self.sql_executor.calc_count(conditions)
            self.mc.set(ck, ret, ONE_HOUR)
            return ret
        else:
            return c

    def gets_custom(self, func, a, kw):
        func_name = func.func_name
        cfg = self.config_mgr.lookup_custom([func_name,]+kw.keys())
        ck = cfg.to_string(kw)
        ret = self.mc.get(ck)
        if ret is None:
            ret = func(self.cls, *a, **kw)
            self.mc.set(ck, ret)
        return ret
