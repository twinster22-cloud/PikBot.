(() => {
  const svg = 'http://www.w3.org/2000/svg';
  const football = [[345,65],[365,100],[370,130],[370,157],[370,183],[370,210],
    [370,237],[345,293],[323,183],[295,155],[295,218]];
  const hockey = [[69,190],[149,132],[149,248],[233,93],[265,190],[233,287]];
  const baseball = [[400,351],[400,237],[288,212],[514,210],[340,165],
    [462,165],[255,106],[400,74],[545,106],[420,323]];
  const fight = [[326,187],[474,187]];
  const make = (tag, attrs) => {
    const el = document.createElementNS(svg, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    return el;
  };
  const mirror = spots => spots.map(([x, y]) => [800 - x, y]);
  const place = (el, x, y) => el.setAttribute('transform', `translate(${x} ${y})`);
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  function avatar(side, surface) {
    const body = make('g', {class:`field-avatar ${side ? 'home' : 'away'}`});
    const combat = surface === 'fight';
    const helmet = surface === 'football' || surface === 'hockey';
    const arm = make('path', {d:'M7 -16 13 -10 10 -4', fill:'none',
      stroke:combat ? '#d0ab88' : '#e6e8fa', 'stroke-width':4, 'stroke-linecap':'round'});
    const legs = make('path', {d:'M-4 -3 -7 4M4 -3 7 4', stroke:'#f4efff',
      'stroke-width':4.5, 'stroke-linecap':'round'});
    body.append(
      make('ellipse', {cy:6, rx:11, ry:4, fill:'#141321', opacity:.45}),
      legs,
      make('path', {d:'M-9 -18 -14 -11 -10 -5', fill:'none', stroke:'#e6e8fa',
        'stroke-width':4, 'stroke-linecap':'round'}),
      arm,
      make('path', {d:helmet ? 'M-11 -21H11L9 -8 6 -3H-6L-9 -8Z' : 'M-8 -21H8L7 -7 4 -3H-4L-7 -7Z',
        stroke:'#f8f0ff', 'stroke-width':1.1, 'paint-order':'stroke',
        ...(combat ? {fill:'#d0ab88'} : {})}),
      make('path', {d:'M-5 -7H5L6 -3H-6Z'}),
      make('circle', {cy:-25, r:5.5, fill:helmet ? 'inherit' : '#d0ab88',
        stroke:helmet ? '#f4efff' : '#4a2f36', 'stroke-width':1.2})
    );
    if (surface === 'hockey') body.append(make('path', {d:'M10 -9 17 6h5',
      fill:'none', stroke:'#293049', 'stroke-width':2.4}));
    return {body, arm, legs};
  }

  function fieldAction(surface, play, side) {
    const text = `${play.kind || ''} ${play.text || ''}`;
    const direction = side ? -1 : 1;
    if (surface === 'football') {
      if (!/pass|rush|scrambl|sack|punt|kick|run|kneel|spike|touchdown/i.test(text)) return;
      const pass = /pass|punt|kick/i.test(text);
      const advance = clamp(Number.isFinite(play.yards) ? play.yards * 4.5 : pass ? 92 : 31, -75, 220);
      return {actor:side * football.length + 8, movement:pass ? 31 : advance,
        direction, pose:pass ? 'throw' : 'run', from:[side ? 477 : 323, 180],
        to:[400 + direction * advance, pass ? 95 : 197], lift:pass ? 54 : 0};
    }
    if (surface === 'hockey') {
      if (!play.shot && !/faceoff|goal|shot|save|blocked|missed/i.test(text)) return;
      const shot = !/faceoff/i.test(text);
      return {actor:side * hockey.length + 4, movement:shot ? 94 : 15, direction, pose:shot ? 'sweep' : 'run',
        from:[400 - direction * 97, 176], to:[shot ? 400 + direction * 324 : 400, 190], lift:0};
    }
    if (surface === 'baseball') {
      if (!/pitch|strike|ball|single|double|triple|home run|homer|ground|fly|line|foul|walk|out/i.test(text)) return;
      const hit = /singles?|doubles?|triples?|home run|homers?/i.test(text);
      const swing = hit || /foul|swing|in play/i.test(text);
      return {actor:9, movement:hit ? 83 : 0, vertical:hit ? -92 : 0, direction:1,
        pitch:[400,218], from:[400,327], to:hit ? [508,125] : null, swing, lift:hit ? 60 : 10};
    }
  }

  window.mountFieldReplay = (host, surface, motionPreference, container, stage, onReady) => {
    const positions = {
      football:[...football, ...mirror(football)],
      hockey:[...hockey, ...mirror(hockey)],
      baseball,
      fight
    }[surface];
    if (!positions) return;
    host.setAttribute('hidden', '');
    const half = positions.length / 2;
    const players = positions.map(([x, y], index) => {
      const player = avatar(surface === 'baseball' ? Number(index === 9) : Number(index >= half), surface);
      place(player.body, x, y);
      host.append(player.body);
      return player;
    });
    const ball = make(surface === 'football' ? 'ellipse' : 'circle',
      surface === 'football' ? {rx:6,ry:3,fill:'#92522e',stroke:'#fff1d7','stroke-width':1.2}
        : {r:surface === 'hockey' ? 5 : 4,fill:surface === 'hockey' ? '#191924' : '#fff6df',stroke:'#25202f'});
    ball.setAttribute('hidden', '');
    if (surface !== 'fight') host.append(ball);
    let previousAction;
    let frameId;
    let replayTimer;
    let field3D;
    let loading3D = false;
    const sideFor = index => surface === 'baseball'
      ? Number(players[index].body.classList.contains('home')) : Number(index >= half);
    const points = positions.map(([x,y])=>({x,y}));
    const movePlayer = (index,x,y,facing,pose,jump=0) => {
      place(players[index].body,x,y);
      points[index] = {x,y};
      field3D?.player(index,points[index],facing,pose,jump);
    };

    const update = (data, paused) => {
      clearTimeout(replayTimer);
      host.toggleAttribute('hidden', data.teams.length !== 2);
      if (host.hasAttribute('hidden')) return;
      const activeRound = surface === 'fight' && data.state === 'in';
      const action = surface === 'fight'
        ? activeRound && {key:`round-${data.period || 'active'}`}
        : data.plays.map(play => {
            const side = data.teams.findIndex(team => team.id === play.team);
            const movement = side >= 0 || surface === 'baseball' ? fieldAction(surface, play, Math.max(side, 0)) : null;
            return movement && {...movement, scoring:play.scoring, key:play.id || [play.period,play.clock,play.text].join('|'), side};
          }).find(Boolean);
      if (surface === 'baseball') {
        for (const [index, player] of players.entries()) {
          const battingSide = action?.side === 0 ? 0 : 1;
          const side = index === 9 ? battingSide : 1 - battingSide;
          player.body.classList.toggle('home', side === 1);
          player.body.classList.toggle('away', side === 0);
        }
      }
      if (!loading3D && container) {
        loading3D = true;
        import('/livecast-3d.js?v=photo-motion').then(({mountCourt}) => {
          field3D = mountCourt(container,stage,data.teams,motionPreference,onReady,
            {surface,sides:players.map((_,index)=>sideFor(index))});
          points.forEach((point,index)=>field3D.player(index,point,sideFor(index)?-1:1,'ready',0));
          field3D.motion(data.state==='in'&&!paused);
          if(action?.scoring)field3D.cheer();
        }).catch(()=>{});
      }
      field3D?.motion(data.state==='in'&&!paused);
      if (surface==='baseball' && field3D) players.forEach((_,index)=>field3D.side(index,sideFor(index)));
      if (!action || paused || data.state === 'pre') {
        cancelAnimationFrame(frameId);
        previousAction = undefined;
        ball.setAttribute('hidden', '');
        field3D?.ball(null);
        players.forEach((player, index) => {
          movePlayer(index,...positions[index],sideFor(index)?-1:1,'ready');
          player.legs.removeAttribute('transform');
          player.arm.removeAttribute('transform');
        });
        return;
      }
      if (action.key === previousAction) return;
      previousAction = action.key;
      if(action.scoring)field3D?.cheer();
      cancelAnimationFrame(frameId);
      const started = performance.now();
      const frame = now => {
        const elapsed = Math.max(0, now - started);
        const sparring = surface === 'fight' && !motionPreference.matches;
        const progress = motionPreference.matches ? 1 : sparring ? (elapsed % 4200) / 4200 : clamp(elapsed / 1750, 0, 1);
        const pace = Math.sin(progress * Math.PI);
        const baseball = surface === 'baseball';
        const run = baseball ? clamp((progress - .43) / .57, 0, 1) : pace;
        players.forEach((player, index) => {
          const [x, y] = positions[index];
          const featured = index === action.actor;
          const move = surface === 'fight' ? (index ? -1 : 1) * 37
            : featured ? action.direction * action.movement
            : baseball ? 0 : (index < half ? 1 : -1) * (index % 3 + 1) * 8;
          const stride = Math.sin(progress * Math.PI * 8) * pace * 6;
          const vertical = sparring ? Math.sin(progress*Math.PI*2) * (index ? -15 : 15)
            : featured ? (action.vertical || 0) * run : 0;
          const lift = featured ? Math.abs(stride) : 0;
          const pose = baseball
            ? index === 1 && progress < .28 ? 'pitch'
              : featured && action.swing && progress > .22 && progress < .49 ? 'swing' : 'ready'
            : featured && progress > .12 && progress < .52 ? action.pose : 'ready';
          movePlayer(index,x + move*run,y + vertical,
            move ? Math.sign(move) : sideFor(index)?-1:1,
            pose,lift*.35);
          player.legs.setAttribute('transform', `rotate(${move ? stride * 3 : 0})`);
          player.arm.setAttribute('transform', `rotate(${featured ? pace * -35 : 0} 7 -16)`);
        });
        if (action.from) {
          const pitching = baseball && progress < .34;
          const from = pitching ? action.pitch : action.from;
          const to = pitching ? action.from : action.to;
          const travel = pitching ? progress / .34 : baseball ? (progress - .34) / .66 : Math.min(1,progress / .8);
          const visible = Boolean(to) && progress < 1;
          if (visible) {
            const x = from[0] + (to[0] - from[0]) * travel;
            const y = from[1] + (to[1] - from[1]) * travel;
            const arc = Math.sin(Math.PI * travel) * (pitching ? 9 : action.lift);
            place(ball,x,y-arc);
            field3D?.ball({x,y},.19+arc*.065);
          } else field3D?.ball(null);
          ball.toggleAttribute('hidden',!visible);
        }
        if (progress < 1) frameId = requestAnimationFrame(frame);
        else if (!motionPreference.matches && !paused && data.state === 'in') {
          // Loop the latest play visualization while waiting for new data.
          // Replays do not represent live player-tracking coordinates.
          replayTimer = setTimeout(() => {
            previousAction = undefined;
            update(data, paused);
          }, 2200);
        }
      };
      frameId = requestAnimationFrame(frame);
    };
    return update;
  };
})();
