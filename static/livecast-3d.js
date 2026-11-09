import {
  ACESFilmicToneMapping, BoxGeometry, CanvasTexture, CapsuleGeometry, Color,
  CylinderGeometry, DirectionalLight, DoubleSide, Fog, Group, HemisphereLight,
  Mesh, MeshBasicMaterial, MeshStandardMaterial,
  PCFShadowMap, PerspectiveCamera, PlaneGeometry, RepeatWrapping, Scene,
  SphereGeometry, Sprite, SpriteMaterial, SRGBColorSpace, TorusGeometry,
  Vector3, WebGLRenderer
} from '/livecast-three.js';

const PI = Math.PI;
const position = (object, x, y, z) => object.position.set(x, y, z);
const courtX = x => (x - 400) / 26;
const courtZ = y => (y - 190) / 21.6;
const blend = (from, to, alpha) => from + (to - from) * alpha;
const surface = color => new MeshStandardMaterial({color, roughness:.74});
const box = new BoxGeometry(1, 1, 1);
const sphere = new SphereGeometry(1, 24, 18);

const limb = new CapsuleGeometry(1, 2, 3, 9);
const cone = new CylinderGeometry(.87, 1, 1, 14);

// Ring profiles from hips to shoulders and from the cuff to the waist.
function tailoredMesh(profile) {
  const mesh = new CylinderGeometry(1,1,1,24,profile.length-1);
  const points = mesh.attributes.position;
  for (let i = 0; i < points.count; i++) {
    const y = points.getY(i);
    const [width, depth] = profile[Math.round((y+.5)*(profile.length-1))];
    points.setXYZ(i,points.getX(i)*width,y,points.getZ(i)*depth);
  }
  mesh.computeVertexNormals();
  return mesh;
}
const torsoMesh = tailoredMesh([[.30,.162],[.27,.152],[.26,.147],[.278,.165],[.322,.19],[.346,.18],[.289,.142],[.16,.108]]);
const shortsMesh = tailoredMesh([[.118,.12],[.141,.139],[.15,.145],[.147,.142],[.136,.136]]);
const cuffMesh = new TorusGeometry(1,.035,7,25);
const noseMesh = new SphereGeometry(1,12,10);

function jerseyFabric() {
  const image = canvas(64,64);
  const ctx = image.getContext('2d');
  ctx.fillStyle = '#f2f2f2';
  ctx.fillRect(0,0,64,64);
  for (let y=0;y<64;y+=3) {
    ctx.fillStyle = y%2 ? '#ffffff' : '#cfcfcf';
    for (let x=(y%2)*2;x<64;x+=4) ctx.fillRect(x,y,2,1);
  }
  const map = texture(image);
  map.wrapS = map.wrapT = RepeatWrapping;
  map.repeat.set(4,3);
  return map;
}

function model(parent, geometry, material, size, xyz, shadows = true) {
  const mesh = new Mesh(geometry, material);
  mesh.scale.set(...size);
  position(mesh, ...xyz);
  mesh.castShadow = shadows;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}
function group(parent, x, y, z) {
  const part = new Group();
  position(part, x, y, z);
  parent.add(part);
  return part;
}
function canvas(w, h) {
  const source = document.createElement('canvas');
  source.width = w;
  source.height = h;
  return source;
}
function texture(source) {
  const map = new CanvasTexture(source);
  map.colorSpace = SRGBColorSpace;
  return map;
}
function random(seed) {
  let value = seed >>> 0;
  return () => {
    value = (Math.imul(1664525, value) + 1013904223) >>> 0;
    return value / 4294967296;
  };
}
function kitColors(teams) {
  const palette = teams.map((team, index) => new Color(/^[a-f\d]{6}$/i.test(team.color) ? '#' + team.color : (index ? '#68429c' : '#b8244e')));
  if (Math.hypot(palette[0].r - palette[1].r, palette[0].g - palette[1].g, palette[0].b - palette[1].b) < .24) {
    palette[0].set('#f5f1ff');
  }
  return palette;
}

function paintCourt(source, home, colors) {
  const ctx = source.getContext('2d');
  const rng = random(16);
  const width = source.width, height = source.height;
  for (let y = 0; y < height; y += 11) {
    let x = -Math.floor(rng() * 160);
    while (x < width) {
      const board = 85 + rng() * 230;
      const shade = Math.floor(rng() * 34);
      ctx.fillStyle = 'rgb(' + (197 + shade) + ' ' + (145 + shade) + ' ' + (96 + shade) + ')';
      ctx.fillRect(x, y, board + 1, 11);
      ctx.fillStyle = '#5c362120';
      ctx.fillRect(x, y, 1, 11);
      ctx.fillRect(x, y + 10, board, 1);
      for (let grain = 0; grain < 4; grain++) ctx.fillRect(x + rng() * board * .5, y + rng() * 10, board * .35, .35);
      x += board;
    }
  }
  const sx = width / 28, sz = height / 15;
  const px = x => (x + 14) * sx, py = z => (z + 7.5) * sz;
  ctx.fillStyle = '#' + colors[1].getHexString();
  ctx.globalAlpha = .9;
  for (const side of [-1, 1]) {
    ctx.fillRect(px(side < 0 ? -14 : 8.2), py(-2.45), 5.8 * sx, 4.9 * sz);
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = '#fff6e8';
  ctx.lineWidth = 4;
  ctx.strokeRect(3, 3, width - 6, height - 6);
  function line(x1, z1, x2, z2) {
    ctx.beginPath();
    ctx.moveTo(px(x1), py(z1));
    ctx.lineTo(px(x2), py(z2));
    ctx.stroke();
  }
  function circle(x, z, radius, from = 0, to = PI * 2) {
    ctx.beginPath();
    ctx.ellipse(px(x), py(z), radius * sx, radius * sz, 0, from, to);
    ctx.stroke();
  }
  line(0, -7.5, 0, 7.5);
  circle(0, 0, 1.8);
  for (const side of [-1, 1]) {
    const freeThrow = side * 8.2, base = side * 14;
    line(base, -2.45, freeThrow, -2.45);
    line(freeThrow, -2.45, freeThrow, 2.45);
    line(base, 2.45, freeThrow, 2.45);
    circle(freeThrow, 0, 1.8, side < 0 ? -PI / 2 : PI / 2, side < 0 ? PI / 2 : PI * 1.5);
    const rim = 12.75, arc = 6.75, cut = Math.asin(6.2 / arc), end = rim - Math.cos(cut) * arc;
    line(base, -6.2, side * end, -6.2);
    line(base, 6.2, side * end, 6.2);
    circle(side * rim, 0, arc, side < 0 ? -cut : PI - cut, side < 0 ? cut : PI + cut);
  }
  ctx.fillStyle = '#' + colors[1].getHexString();
  ctx.beginPath();
  ctx.ellipse(px(0), py(0), 1.48 * sx, 1.48 * sz, 0, 0, PI * 2);
  ctx.fill();
  ctx.font = '900 51px system-ui';
  ctx.fillStyle = 'white';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(home.short || 'HOME').slice(0, 5), px(0), py(0));
}

function buildArena(scene, colors, home) {
  const courtImage = canvas(1536, 832);
  paintCourt(courtImage, home, colors);
  const courtMap = texture(courtImage);
  courtMap.anisotropy = 4;
  const court = model(scene, new PlaneGeometry(28, 15), new MeshStandardMaterial({map:courtMap, roughness:.31, metalness:.04}), [1,1,1], [0,.014,0], false);
  court.rotation.x = -PI / 2;
  model(scene, box, surface(colors[1].clone().multiplyScalar(.38)), [32,.2,18.2], [0,-.104,0], false);
  model(scene, box, surface('#100d18'), [72,.25,60], [0,-.24,0], false);
  const board = new MeshStandardMaterial({color:'#b5c8e0', transparent:true, opacity:.34, roughness:.21, side:DoubleSide});
  const steel = surface('#414964'), rimPaint = surface('#ff602f'), padding = surface(colors[1]);
  const netPaint = new MeshBasicMaterial({color:'#eeeaff', transparent:true, opacity:.75});
  for (const side of [-1, 1]) {
    const end = 13.26 * side;
    model(scene, box, padding, [.91,1.42,1.01], [15 * side,.70,0]);
    const stand = model(scene, box, steel, [.23,2.86,.28], [14.93 * side,2.08,0]);
    stand.rotation.z = side*.21;
    model(scene, box, steel, [1.13,.13,.12], [14.43 * side,3.59,0]);
    model(scene, box, board, [.075,1.06,1.8], [13.86 * side,3.43,0]);
    for (const z of [-.89,.89]) model(scene, box, steel, [.085,1.1,.03], [13.85 * side,3.43,z]);
    for (const y of [2.89,3.97]) model(scene, box, steel, [.085,.03,1.82], [13.85 * side,y,0]);
    const rim = model(scene, new TorusGeometry(.23,.024,7,24), rimPaint, [1,1,1], [end,3.05,0]);
    rim.rotation.x = -PI / 2;
    const bottom = model(scene, new TorusGeometry(.13,.007,4,16), netPaint, [1,1,1], [end,2.68,0], false);
    bottom.rotation.x = -PI / 2;
    for (let j = 0; j < 12; j++) {
      const angle = j * PI / 6;
      const strand = model(scene, box, netPaint, [.009,.385,.009], [end + Math.cos(angle) * .18,2.86,Math.sin(angle) * .18], false);
      strand.rotation.set(Math.sin(angle) * .25, 0, -Math.cos(angle) * .25);
    }
  }

  const boardImage = canvas(1024, 96);
  const ctx = boardImage.getContext('2d');
  const glow = ctx.createLinearGradient(0,0,1024,96);
  glow.addColorStop(0,'#200c47'); glow.addColorStop(.5,'#57309a'); glow.addColorStop(1,'#180b37');
  ctx.fillStyle = glow; ctx.fillRect(0,0,1024,96);
  ctx.fillStyle = '#a769ed'; ctx.fillRect(0,2,1024,3); ctx.fillRect(0,91,1024,3);
  ctx.font = 'italic 900 30px system-ui';
  ctx.textAlign = 'center'; ctx.fillStyle = '#f4e6ff';
  for (let i = 0; i < 4; i++) ctx.fillText('PIKBOT LIVECAST', 128 + i * 256, 59);
  const ribbon = new MeshBasicMaterial({map:texture(boardImage)});
  model(scene, new PlaneGeometry(33,1.2), ribbon, [1,1,1], [0,.66,-8.93], false);
  model(scene, box, surface('#1b1728'), [34,1.2,.55], [0,.59,-9.24], false);
  const black = surface('#17121e');
  model(scene, box, black, [64,13,.6], [0,6.5,-11.3], false);
  const crowdMaterial = new MeshBasicMaterial({color:'#392d45',toneMapped:false});
  model(scene,new PlaneGeometry(57,19),crowdMaterial,[1,1,1],[0,9.5,-10.9],false);
  const backdrop = new Image();
  backdrop.onload = () => {
    crowdMaterial.color.set('#ffffff');
    crowdMaterial.map = texture(backdrop);
    crowdMaterial.needsUpdate = true;
  };
  backdrop.src = '/livecast-arena.webp';
  for (const side of [-1,1]) {
    const bench = group(scene,side*10.5,0,-8.57);
    for (let i=0;i<7;i++) {
      const x = (i-3)*.63;
      model(bench,box,black,[.48,.10,.47],[x,.45,0],false);
      const seat = model(bench,box,black,[.48,.46,.09],[x,.69,-.18],false);
      seat.rotation.x = -.10;
    }
  }
  const emblem = typeof home.logo === 'string' && home.logo.startsWith('https://a.espncdn.com/') ? new Image() : null;
  if (emblem) {
    emblem.crossOrigin = 'anonymous';
    emblem.onload = () => {
      const ctx = courtImage.getContext('2d');
      const x = courtImage.width/2, y = courtImage.height/2;
      ctx.save();
      ctx.beginPath();
      ctx.arc(x,y,81,0,PI*2);
      ctx.fillStyle = '#' + colors[1].getHexString();
      ctx.fill();
      ctx.clip();
      ctx.drawImage(emblem,x-78,y-78,156,156);
      ctx.restore();
      courtMap.needsUpdate = true;
    };
    emblem.src = home.logo;
  }
}

function buildAthlete(scene, index, jersey) {
  const root = group(scene,0,0,0);
  const rig = group(root,0,0,0);
  const skin = surface('#ac714d'), hair = surface('#201715');
  const shoe = surface(index % 3 === 0 ? '#b3a8cb' : '#e8e4ec');
  const trim = surface('#ede7f4');
  const body = group(rig,0,1.15,0);
  model(body, torsoMesh, jersey, [1,.64,1], [0,.265,0]);
  const hips = model(rig, sphere, jersey, [.28,.15,.16], [0,1.15,0]);
  hips.rotation.x = -.12;
  const collar = model(body,cuffMesh,trim,[.108,.074,.16],[0,.585,0]);
  collar.rotation.x = -PI / 2;
  model(body,limb,skin,[.103,.062,.10],[0,.615,0]);
  const face = group(body,0,.77,.005);
  const head = model(face,sphere,skin,[.146,.188,.132],[0,0,0]);
  model(face,sphere,skin,[.122,.102,.112],[0,-.074,.022]);
  model(face,sphere,hair,[.148,.096,.136],[0,.099,-.016]);
  model(face,sphere,hair,[.147,.116,.084],[0,.018,-.065]);
  model(face,noseMesh,skin,[.035,.037,.047],[0,-.006,.132]);
  const eyes = surface('#251b1b'), lips = surface('#754437');
  model(face,noseMesh,lips,[.046,.011,.01],[0,-.066,.125]);
  for (const side of [-1,1]) {
    model(face,sphere,skin,[.036,.052,.033],[side*.143,-.008,-.002]);
    model(face,noseMesh,eyes,[.020,.010,.006],[side*.055,.033,.125]);
    model(face,noseMesh,hair,[.026,.008,.005],[side*.055,.056,.123]);
  }

  const arms = [], legs = [];
  for (const side of [-1,1]) {
    const shoulder = group(body,side*.313,.45,0);
    model(shoulder,sphere,skin,[.121,.133,.116],[0,.013,-.005]);
    const strap = model(shoulder,sphere,jersey,[.082,.10,.112],[-side*.037,.045,-.018]);
    strap.rotation.z = -side*.18;
    model(shoulder,limb,skin,[.084,.106,.087],[0,-.145,0]);
    const elbow = group(shoulder,0,-.32,0);
    model(elbow,limb,skin,[.068,.088,.069],[0,-.114,0]);
    model(elbow,sphere,skin,[.065,.09,.052],[0,-.28,.017]);
    arms.push({shoulder,elbow});

    const hip = group(rig,side*.157,1.13,0);
    model(hip,shortsMesh,jersey,[1,.37,1],[0,-.135,0]);
    const hem = model(hip,cuffMesh,trim,[.115,.12,.14],[0,-.314,0]);
    hem.rotation.x = -PI / 2;
    model(hip,limb,skin,[.093,.087,.092],[0,-.335,0]);
    const knee = group(hip,0,-.50,0);
    model(knee,limb,skin,[.086,.118,.079],[0,-.157,0]);
    model(knee,cone,trim,[.094,.20,.088],[0,-.387,0]);
    model(knee,sphere,shoe,[.111,.073,.183],[0,-.475,.089]);
    model(knee,sphere,trim,[.108,.025,.184],[0,-.525,.088]);
    const ankle = model(knee,sphere,shoe,[.103,.092,.105],[0,-.433,-.004]);
    ankle.rotation.x = -.23;
    for (let lace=0;lace<3;lace++) model(knee,box,trim,[.098,.009,.018],[0,-.408-lace*.007,.079+lace*.031],false);
    legs.push({hip,knee});
  }

  const numberCanvas = canvas(128,128);
  const numberMap = texture(numberCanvas);
  const numberMat = new MeshBasicMaterial({map:numberMap,transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-1});
  const numberPlane = new PlaneGeometry(.34,.34);
  model(body,numberPlane,numberMat,[1,1,1],[0,.29,.204],false);
  const back = model(body,numberPlane,numberMat,[1,1,1],[0,.29,-.204],false);
  back.rotation.y = PI;

  const labelCanvas = canvas(512,96);
  const labelMap = texture(labelCanvas);
  const label = new Sprite(new SpriteMaterial({map:labelMap,transparent:true,depthTest:false,depthWrite:false}));
  label.renderOrder = 9;
  label.scale.set(2.18,.41,1);
  position(label,0,2.55,0);
  label.visible = false;
  root.add(label);
  return {root,rig,body,head,skin,hair,arms,legs,numberCanvas,numberMap,labelCanvas,labelMap,label,jersey,
          target:new Vector3(),facing:1,pose:'ready',jump:0,actor:false,identity:null,index};
}

function identify(athlete, person) {
  const key = person ? [person.id,person.short,person.name,person.number].join('|') : '';
  if (key === athlete.identity) return;
  athlete.identity = key;
  let seed = athlete.index + 1;
  for (const ch of String(person?.id || '')) seed = (Math.imul(seed,31)+ch.charCodeAt(0)) >>> 0;
  const rng = random(seed);
  const skin = ['#613827','#8f553c','#a97050','#c79674','#d6a98d','#b77d5e'];
  athlete.skin.color.set(skin[Math.floor(rng()*skin.length)]);
  athlete.hair.color.set(['#1a1517','#28201e','#352923'][Math.floor(rng()*3)]);
  athlete.head.scale.x = .137 + rng() * .028;
  athlete.rig.scale.set(.86 + rng()*.25,.91 + rng()*.17,.89 + rng()*.17);
  const ctx = athlete.numberCanvas.getContext('2d');
  ctx.clearRect(0,0,128,128);
  if (person?.number != null) {
    ctx.font = '900 105px system-ui';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineWidth = 10;
    ctx.strokeStyle = '#150b22'; ctx.fillStyle = '#ffffff';
    ctx.strokeText(String(person.number),64,67); ctx.fillText(String(person.number),64,67);
  }
  athlete.numberMap.needsUpdate = true;
  const tag = athlete.labelCanvas.getContext('2d');
  tag.clearRect(0,0,512,96);
  if (person) {
    tag.fillStyle = '#100a20ed'; tag.fillRect(0,4,512,88);
    tag.fillStyle = '#e7c9ff'; tag.fillRect(0,4,512,5);
    tag.fillStyle = '#ffffff'; tag.font = '800 43px system-ui'; tag.textAlign = 'center'; tag.textBaseline = 'middle';
    tag.fillText((person.number != null ? '#' + person.number + ' ' : '') + (person.short || person.name),256,48,474);
  }
  athlete.labelMap.needsUpdate = true;
}

function animateAthlete(athlete, t, alpha, reduced) {
  const {root,target,rig,body,arms,legs,index,pose} = athlete;
  const dx = target.x - root.position.x, dz = target.z - root.position.z;
  const moving = !reduced && Math.hypot(dx,dz) > .13;
  root.position.lerp(target, reduced ? 1 : alpha);
  const turn = moving ? Math.atan2(dx,dz) : athlete.facing * PI / 2;
  const delta = Math.atan2(Math.sin(turn - root.rotation.y),Math.cos(turn - root.rotation.y));
  root.rotation.y += delta * (reduced ? 1 : Math.min(1,alpha*.75));
  const gait = Math.sin(t * 13.8 + index * .8);
  const idle = reduced ? 0 : Math.sin(t * 2.15 + index * 1.8);
  const lift = reduced ? 0 : athlete.jump / 24;
  rig.position.y = lift + (moving ? Math.abs(gait)*.06 : idle*.013);
  body.rotation.x = blend(body.rotation.x, pose === 'shot' ? -.12 : moving ? .16 : .085, alpha);
  body.rotation.z = moving ? gait*.07 : idle*.019;
  arms.forEach(({shoulder,elbow},i) => {
    const stride = (i ? -1 : 1) * gait;
    shoulder.rotation.x = blend(shoulder.rotation.x,pose === 'shot' ? -2.4 : moving ? stride*.86 : -.29 + idle*.045,alpha);
    shoulder.rotation.z = (i ? 1 : -1) * (pose === 'shot' ? -.10 : -.19);
    elbow.rotation.x = blend(elbow.rotation.x,pose === 'shot' ? -.34 : moving ? -.83 : -.57,alpha);
  });
  legs.forEach(({hip,knee},i) => {
    const stride = (i ? 1 : -1) * gait;
    hip.rotation.x = blend(hip.rotation.x,moving ? stride*.72 : -.09,alpha);
    knee.rotation.x = blend(knee.rotation.x,moving ? Math.max(0,-stride)*1.04 + .16 : .13,alpha);
  });
  athlete.label.visible = athlete.actor && Boolean(athlete.identity);
}

function makeBall(scene) {
  const image = canvas(256,128);
  const ctx = image.getContext('2d');
  const rng = random(44);
  ctx.fillStyle = '#eb842c'; ctx.fillRect(0,0,256,128);
  ctx.fillStyle = '#b45518';
  for (let i=0;i<1600;i++) ctx.fillRect(rng()*256,rng()*128,1,1);
  ctx.strokeStyle = '#422415'; ctx.lineWidth = 4;
  ctx.beginPath(); ctx.moveTo(0,64); ctx.lineTo(256,64); ctx.moveTo(128,0); ctx.lineTo(128,128);
  ctx.moveTo(34,0); ctx.bezierCurveTo(89,30,89,99,34,128);
  ctx.moveTo(222,0); ctx.bezierCurveTo(167,30,167,99,222,128); ctx.stroke();
  const map = texture(image); map.wrapS = RepeatWrapping;
  return model(scene,sphere,new MeshStandardMaterial({map,roughness:.79}),[.15,.15,.15],[0,.18,0]);
}

export function mountCourt(container, stage, teams, reducedMotion, onReady) {
  const colors = kitColors(teams);
  const renderer = new WebGLRenderer({antialias:true,alpha:false,powerPreference:'low-power'});
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1,1.5));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFShadowMap;
  renderer.toneMapping = ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.03;
  renderer.domElement.setAttribute('aria-hidden','true');
  container.append(renderer.domElement);
  const scene = new Scene();
  scene.background = new Color('#141019');
  scene.fog = new Fog('#141019',43,88);
  const camera = new PerspectiveCamera(42,1,.15,95);
  camera.position.set(0,15,22);
  const look = new Vector3(0,0,0), focus = new Vector3(), nextCamera = new Vector3();
  scene.add(new HemisphereLight('#f4efff','#76584f',1.64));
  const lamp = new DirectionalLight('#fff1e1',3.05);
  lamp.position.set(-4,16,7);
  lamp.castShadow = true;
  lamp.shadow.mapSize.set(1024,1024);
  Object.assign(lamp.shadow.camera,{left:-19,right:19,top:14,bottom:-14,near:1,far:39});
  lamp.shadow.bias = -.00036;
  lamp.shadow.normalBias = .025;
  scene.add(lamp);
  const fill = new DirectionalLight('#bdccff',1.13);
  fill.position.set(-14,10,-12);
  scene.add(fill);
  buildArena(scene,colors,teams[1]);
  const fabric = jerseyFabric();
  const jerseys = colors.map(color => new MeshStandardMaterial({color,map:fabric,roughness:.88}));
  const players = Array.from({length:10},(_,index)=>buildAthlete(scene,index,jerseys[Math.floor(index/5)]));
  const ball = makeBall(scene);
  const ballTarget = new Vector3(0,.18,0);
  ball.visible = false;
  const observed = new IntersectionObserver(([entry]) => {visible = entry.isIntersecting;});
  observed.observe(stage);
  const resize = new ResizeObserver(() => size());
  resize.observe(container);
  let visible = true, ready = false, animation, last = 0, playActive = false, renderFailed = false;
  function size() {
    const width = container.clientWidth, height = container.clientHeight;
    if (!width || !height) return false;
    renderer.setSize(width,height,false);
    camera.aspect = width/height;
    camera.updateProjectionMatrix();
    return true;
  }
  function failure() {
    renderFailed = true;
    cancelAnimationFrame(animation);
    delete stage.dataset.webgl;
    onReady(false);
    container.replaceChildren();
    observed.disconnect();
    resize.disconnect();
    renderer.dispose();
  }
  renderer.domElement.addEventListener('webglcontextlost',failure,{once:true});
  function render(time) {
    animation = requestAnimationFrame(render);
    if (!visible || document.hidden || renderFailed) return;
    if (!ready && !size()) return;
    const dt = Math.min(.05,(time-last)/1000 || .016);
    last = time;
    const reduced = reducedMotion.matches;
    const pace = 1 - Math.exp(-Math.max(.001,dt)*14);
    const t = time/1000;
    for (const player of players) animateAthlete(player,playActive ? t : 0,pace,reduced);
    ball.position.lerp(ballTarget,reduced ? 1 : Math.min(1,pace*1.7));
    if (!reduced && playActive) {ball.rotation.x += dt*5; ball.rotation.z += dt*2;}
    const mode = stage.dataset.view || 'broadcast';
    const narrow = camera.aspect < 1.52;
    const bx = ball.visible && !reduced ? ball.position.x : 0;
    const bz = ball.visible && !reduced ? ball.position.z : 0;
    const track = Math.max(-7.6,Math.min(7.6,bx*.75));
    const presets = {
      broadcast:[track*.95, narrow ? 8.2 : 9.25, narrow ? 13.2 : 14.6, track, .92, bz*.22],
      sideline:[track-3.2, narrow ? 4.4 : 4.1, narrow ? 11.2 : 10.2, track+.55, 1.3, bz*.22],
      aerial:[0, narrow ? 36 : 31, 2.2, 0, 0, 0],
      wide:[0, narrow ? 21.5 : 17.6, narrow ? 24.5 : 22.1, 0, 0, 0]
    };
    const [cx,cy,cz,fx,fy,fz] = presets[mode] || presets.broadcast;
    nextCamera.set(cx,cy,cz);
    focus.set(fx,fy,fz);
    const cameraPace = ready && !reduced ? 1-Math.exp(-dt*3.1) : 1;
    camera.position.lerp(nextCamera,cameraPace);
    look.lerp(focus,cameraPace);
    camera.lookAt(look);
    try {
      renderer.render(scene,camera);
      if (!ready) {ready = true; stage.dataset.webgl = 'true'; onReady(true);}
    } catch (_) {failure();}
  }
  animation = requestAnimationFrame(render);
  return {
    player(index,point,facing,pose,jump) {
      const player = players[index];
      player.target.set(courtX(point.x),0,courtZ(point.y));
      if (!ready) player.root.position.copy(player.target);
      Object.assign(player,{facing,pose,jump});
    },
    lineup(index,person) {identify(players[index],person);},
    handler(index) {players.forEach(player=>{player.actor = index === player.index;});},
    ball(point,height) {
      ballTarget.set(courtX(point.x),Math.max(.15,height),courtZ(point.y));
      if (!ball.visible) ball.position.copy(ballTarget);
      ball.visible = true;
    },
    motion(active) {playActive = active;}
  };
}
