/* Pure response eligibility rules, shared with offline behavior checks. */
(function (root) {
  'use strict';
  var api = {
    accepts: function (current, requested, body) {
      if (current && typeof current === 'object') {
        if (!requested || current.epoch !== requested.epoch || current.generation !== requested.generation) return false;
        var identity = body && (body.generation != null ? body : body.meta);
        return !!identity && (identity.epoch || '') === (current.epoch || '') && identity.generation === current.generation;
      }
      if (current !== requested) return false;
      var generation = body && (body.generation != null ? body.generation : body.meta && body.meta.generation);
      return current == null ? generation == null : generation === current;
    },
    canSwitch: function (target, options) {
      return target === 'baseline' || options.some(function (o) { return o.id === target && o.ready; });
    },
    errorText: function (status) {
      if (status === 409) return '暂时无法切换，正在同步状态';
      if (status === 403) return '切换凭据已失效，正在同步，请重试';
      return '切换失败，请稍后重试';
    }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SupplyUI = api;
})(typeof window !== 'undefined' ? window : globalThis);
