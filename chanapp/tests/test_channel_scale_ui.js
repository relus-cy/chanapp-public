'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const removed=[],series=[];
const channels=[{upper:{t0:'2026-01-01',t1:'2026-09-01',y0:42,y1:-90000},lower:{t0:'2026-01-01',t1:'2026-09-01',y0:41,y1:-100000}}];
const context={c:{channelSeries:['old'],main:{removeSeries:s=>removed.push(s),addSeries:(type,options)=>{const s={options,setData(data){this.data=data;}};series.push(s);return s;}}},data:{channels},P:{goldA:'gold'},LightweightCharts:{LineSeries:{},LineStyle:{Dashed:2}},toTime:x=>x};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('    c.channelSeries.forEach('),source.indexOf('    if (opts.resetRange !== false)')),context);
assert.deepEqual(removed,['old']);
assert.equal(series.length,2);
for(const [index,s] of series.entries()) {
  assert.equal(typeof s.options.autoscaleInfoProvider,'function','projected channels must override autoscale');
  let defaultCalled=false;
  const result=s.options.autoscaleInfoProvider(()=>{defaultCalled=true;return {priceRange:{minValue:-100000,maxValue:42}};});
  assert.equal(result,null,'channel contributes no price range');
  assert.equal(defaultCalled,false);
  const rail=channels[0][index===0?'upper':'lower'];
  assert.equal(JSON.stringify(s.data),JSON.stringify([{time:rail.t0,value:rail.y0},{time:rail.t1,value:rail.y1}]),'native projected geometry is untouched');
}
console.log('projected channel autoscale exclusion and unmodified geometry checks passed');
