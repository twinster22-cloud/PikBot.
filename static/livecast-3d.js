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

function sculptedHead() {
  const geometry = new SphereGeometry(1,42,32);
  const points = geometry.attributes.position;
  const profile = (x,y,cx,cy,width,height) =>
    Math.exp(-(((x-cx)/width)**2+((y-cy)/height)**2));
  for (let i=0;i<points.count;i++) {
    const x = points.getX(i), y = points.getY(i), z = points.getZ(i);
    const width = 1-.20*profile(0,y,0,-.91,1,.30)+.05*profile(0,y,0,-.39,1,.34);
    const front = Math.max(0,z)**2;
    const nose = .25*profile(x,y,0,-.11,.22,.21)+.12*profile(x,y,0,.13,.17,.33);
    const brows = .08*profile(Math.abs(x),y,.37,.31,.30,.12);
    const socket = .093*profile(Math.abs(x),y,.36,.14,.23,.13);
    const cheek = .10*profile(Math.abs(x),y,.57,-.13,.25,.19);
    const jaw = .11*profile(x,y,0,-.68,.56,.22);
    points.setXYZ(i,x*width,y,z+front*(nose+brows-socket+cheek+jaw));
  }
  geometry.computeVertexNormals();
  return geometry;
}
const headGeometry = sculptedHead();
const faceSwatches = ['#6f452f','#a26e51','#a6775e','#65412c','#b47559','#a06c52'];
let faceMaterials;
let faceTexturePromise;

function loadFaceMaterials() {
  if (faceTexturePromise) return faceTexturePromise;
  faceTexturePromise = new Promise(resolve => {
    const image = new Image();
    image.onload = () => {
      const size = image.naturalWidth/3;
      if (!Number.isInteger(size) || !size || size*2 !== image.naturalHeight) {
        resolve();
        return;
      }
      faceMaterials = faceSwatches.map((_,index) => {
        const tile = canvas(size,size);
        tile.getContext('2d').drawImage(image,index%3*size,Math.floor(index/3)*size,size,size,0,0,size,size);
        return new MeshStandardMaterial({map:texture(tile),roughness:.66});
      });
      resolve();
    };
    image.onerror = () => resolve();
    image.src = '/livecast-face-uv.webp';
  });
  return faceTexturePromise;
}

function applyFaceMaterial(athlete) {
  if (!faceMaterials || athlete.faceStyle == null) return;
  athlete.head.material = faceMaterials[athlete.faceStyle];
  athlete.skin.color.set(faceSwatches[athlete.faceStyle]);
  for (const detail of athlete.fallbackFace) detail.visible = false;
}


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


// One continuous skin surface bends across the joint, so the upper and lower limb don't separate.
function flexibleSkin(parent, material, rings, jointY) {
  const geometry = new CylinderGeometry(1,1,1,14,rings.length-1,true);
  const points = geometry.attributes.position;
  const weights = new Float32Array(points.count);
  for (let i=0;i<points.count;i++) {
    const ring = rings[Math.round((.5-points.getY(i))*(rings.length-1))];
    const [y,width,depth] = ring;
    points.setXYZ(i,points.getX(i)*width,y,points.getZ(i)*depth);
    const t = Math.max(0,Math.min(1,(jointY-y+.09)/.18));
    weights[i] = t*t*(3-2*t);
  }
  geometry.computeVertexNormals();
  const mesh = model(parent,geometry,material,[1,1,1],[0,0,0]);
  mesh.frustumCulled = false;
  return {geometry, points, weights, jointY, bind:points.array.slice(), angle:0};
}
function flexSkin(skin, angle) {
  if (Math.abs(skin.angle-angle) < .035) return;
  skin.angle = angle;
  const c = Math.cos(angle), s = Math.sin(angle);
  for (let i=0;i<skin.points.count;i++) {
    const offset = i*3, x = skin.bind[offset], y = skin.bind[offset+1], z = skin.bind[offset+2];
    const relative = y-skin.jointY, w = skin.weights[i];
    skin.points.setXYZ(i,x,y+w*(relative*c-z*s-relative),z+w*(relative*s+z*c-z));
  }
  skin.points.needsUpdate = true;
  skin.geometry.computeVertexNormals();
}
const armSkinRings = [[.098,.015,.019],[.068,.071,.074],[.025,.101,.098],[-.037,.100,.093],
  [-.103,.090,.081],[-.174,.080,.074],[-.246,.068,.066],[-.319,.066,.065],[-.377,.068,.066],
  [-.437,.061,.058],[-.503,.052,.046],[-.566,.039,.035]];
const legSkinRings = [[-.257,.102,.097],[-.317,.104,.096],[-.375,.096,.089],[-.434,.083,.078],
  [-.498,.078,.079],[-.552,.087,.077],[-.613,.088,.078],[-.677,.077,.071],[-.738,.066,.065],
  [-.800,.059,.055],[-.851,.052,.049]];

function buildAthlete(scene, index, jersey, team) {
  const root = group(scene,0,0,0);
  const rig = group(root,0,0,0);
  const skin = surface('#ac714d'), hair = surface('#201715');
  skin.roughness = .61;
  const shoe = surface(index % 3 === 0 ? '#b3a8cb' : '#e8e4ec');
  const trim = surface('#ede7f4');
  const seam = surface(jersey.color.clone().lerp(new Color('#ece5f7'),.62));
  const body = group(rig,0,1.15,0);
  model(body, torsoMesh, jersey, [1,.64,1], [0,.265,0]);
  const waistband = model(body,cuffMesh,seam,[.268,.158,.33],[0,-.021,0]);
  waistband.rotation.x = -PI / 2;
  for (const side of [-1,1]) {
    const panel = model(body,sphere,seam,[.014,.242,.028],[side*.263,.20,.018],false);
    panel.rotation.z = -side*.105;
  }
  const hips = model(rig, sphere, jersey, [.28,.15,.16], [0,1.15,0]);
  hips.rotation.x = -.12;
  const collar = model(body,cuffMesh,trim,[.108,.074,.16],[0,.585,0]);
  collar.rotation.x = -PI / 2;
  model(body,limb,skin,[.103,.062,.10],[0,.615,0]);
  const face = group(body,0,.747,.006);
  face.scale.set(.84,.86,.88);
  const head = model(face,headGeometry,skin,[.145,.181,.133],[0,0,0]);
  const fallbackFace = [];
  const scalp = model(face,sphere,hair,[.149,.072,.135],[0,.109,-.013],false);
  const sideHair = model(face,sphere,hair,[.147,.109,.083],[0,.02,-.065],false);
  const crown = group(face,0,.145,-.01);
  for (const [x,z] of [[-.097,-.035],[-.053,.04],[0,.065],[.06,.045],[.105,-.025],[-.042,-.059],[.046,-.064]]) {
    model(crown,sphere,hair,[.049,.057,.050],[x,.027,z],false);
  }
  const twists = group(face,0,.10,-.025);
  for (const [x,z] of [[-.112,-.02],[-.066,-.08],[0,-.095],[.067,-.08],[.112,-.018]]) {
    const twist = model(twists,limb,hair,[.031,.045,.031],[x,.045,z],false);
    twist.rotation.z = x * 5;
    twist.rotation.x = -.37;
  }
  const headband = model(face,cuffMesh,trim,[.151,.137,.62],[0,.062,0],false);
  headband.rotation.x = -PI / 2;
  const beard = model(face,sphere,hair,[.107,.056,.075],[0,-.115,.062],false);

  const eyes = surface('#251b1b'), lips = surface('#754437');
  fallbackFace.push(model(face,noseMesh,lips,[.036,.008,.007],[0,-.077,.134],false));
  for (const side of [-1,1]) {
    const ear = model(face,sphere,skin,[.028,.046,.027],[side*.142,-.007,-.006],false);
    ear.rotation.z = -side*.18;
    fallbackFace.push(model(face,noseMesh,eyes,[.016,.007,.005],[side*.055,.035,.125],false));
    fallbackFace.push(model(face,noseMesh,hair,[.023,.006,.004],[side*.055,.058,.124],false));
  }

  const arms = [], legs = [];
  for (const side of [-1,1]) {
    const shoulder = group(body,side*.313,.45,0);
    const armSkin = flexibleSkin(shoulder,skin,armSkinRings,-.32);
    const strap = model(shoulder,sphere,jersey,[.070,.084,.104],[-side*.040,.046,-.018]);
    strap.rotation.z = -side*.18;
    const elbow = group(shoulder,0,-.32,0);
    const wrist = group(elbow,0,-.239,.008);
    model(wrist,sphere,skin,[.058,.063,.034],[0,-.028,.006]);
    model(wrist,sphere,skin,[.044,.048,.023],[0,-.078,.017],false);
    model(wrist,sphere,skin,[.022,.041,.022],[-side*.045,-.036,.012],false);
    arms.push({shoulder,elbow,wrist,skin:armSkin});

    const hip = group(rig,side*.157,1.13,0);
    model(hip,shortsMesh,jersey,[1,.37,1],[0,-.135,0]);
    const hem = model(hip,cuffMesh,trim,[.115,.12,.14],[0,-.314,0]);
    hem.rotation.x = -PI / 2;
    const legSkin = flexibleSkin(hip,skin,legSkinRings,-.50);
    const knee = group(hip,0,-.50,0);
    model(knee,cone,trim,[.094,.20,.088],[0,-.387,0]);
    const foot = group(knee,0,-.505,0);
    model(foot,sphere,shoe,[.111,.073,.183],[0,.030,.089]);
    model(foot,sphere,trim,[.108,.025,.184],[0,-.020,.088]);
    const ankle = model(foot,sphere,shoe,[.103,.092,.105],[0,.072,-.004]);
    ankle.rotation.x = -.23;
    for (let lace=0;lace<3;lace++) model(foot,box,trim,[.098,.009,.018],[0,.097-lace*.007,.079+lace*.031],false);
    legs.push({hip,knee,foot,skin:legSkin});
  }

  const printFront = canvas(192,224), printBack = canvas(192,224);
  const frontMap = texture(printFront), backMap = texture(printBack);
  const printPlane = new PlaneGeometry(.45,.50);
  const printMaterial = map => new MeshStandardMaterial({
    map, transparent:true, roughness:.87, depthWrite:false, polygonOffset:true, polygonOffsetFactor:-1
  });
  model(body,printPlane,printMaterial(frontMap),[1,1,1],[0,.32,.197],false);
  const back = model(body,printPlane,printMaterial(backMap),[1,1,1],[0,.32,-.197],false);
  back.rotation.y = PI;

  const labelCanvas = canvas(512,96);
  const labelMap = texture(labelCanvas);
  const label = new Sprite(new SpriteMaterial({map:labelMap,transparent:true,depthTest:false,depthWrite:false}));
  label.renderOrder = 9;
  label.scale.set(2.18,.41,1);
  position(label,0,2.55,0);
  label.visible = false;
  root.add(label);
  return {root,rig,body,face,head,fallbackFace,skin,hair,scalp,sideHair,crown,twists,headband,beard,shoe,arms,legs,
          printFront,printBack,frontMap,backMap,labelCanvas,labelMap,label,team,
          target:new Vector3(),facing:1,pose:'ready',jump:0,actor:false,identity:null,index,distance:index*.12,dominant:1,crouch:.10};
}

function identify(athlete, person) {
  const key = person ? [person.id,person.short,person.name,person.number,person.position].join('|') : '';
  if (key === athlete.identity) return;
  athlete.identity = key;
  athlete.number = person?.number;
  paintPhotoPlayer(athlete);
  let seed = athlete.index + 1;
  for (const ch of String(person?.id || '')) seed = (Math.imul(seed,31)+ch.charCodeAt(0)) >>> 0;
  const rng = random(seed);
  const skin = ['#613827','#8f553c','#a97050','#c79674','#d6a98d','#b77d5e'];
  athlete.skin.color.set(skin[Math.floor(rng()*skin.length)]);
  athlete.hair.color.set(['#1a1517','#28201e','#352923'][Math.floor(rng()*3)]);
  const style = Math.floor(rng()*5);
  athlete.scalp.visible = style !== 0;
  athlete.sideHair.visible = style > 1;
  athlete.crown.visible = style === 3;
  athlete.twists.visible = style === 4;
  athlete.headband.visible = style === 2;
  athlete.beard.visible = rng() < .37;
  athlete.head.scale.x = .137 + rng() * .023;
  athlete.faceStyle = seed % faceSwatches.length;
  applyFaceMaterial(athlete);
  if (faceMaterials) athlete.beard.visible = false;
  athlete.dominant = seed % 7 === 0 ? 0 : 1;
  athlete.shoe.color.set(['#eceaf1','#141621','#ebe1d1','#8d9aaf','#c5464e'][seed % 5]);
  const position = String(person?.position || '').toUpperCase();
  const build = position.startsWith('C') ? [1.09,1.075,1.07] :
    position.startsWith('G') || /[PS]G/.test(position) ? [.89,.94,.93] : [1.00,1.01,.99];
  athlete.rig.scale.set(build[0]+rng()*.12-.06,build[1]+rng()*.06-.03,build[2]+rng()*.12-.06);
  const lastName = person?.short?.replace(/^(?:[A-Z]\.\s*)+/i,'') || person?.name?.split(' ').at(-1);
  for (const [image,map,heading] of [[athlete.printFront,athlete.frontMap,athlete.team.short],
                                     [athlete.printBack,athlete.backMap,lastName]]) {
    const ink = image.getContext('2d');
    ink.clearRect(0,0,image.width,image.height);
    if (person?.number != null) {
      ink.textAlign = 'center';
      ink.textBaseline = 'middle';
      ink.lineJoin = 'round';
      ink.strokeStyle = '#1b1028';
      ink.fillStyle = '#ffffff';
      ink.font = '900 101px system-ui';
      ink.lineWidth = 10;
      ink.strokeText(String(person.number),96,132);
      ink.fillText(String(person.number),96,132);
      if (heading) {
        ink.font = '900 29px system-ui';
        ink.lineWidth = 5;
        ink.strokeText(String(heading).toUpperCase(),96,64,184);
        ink.fillText(String(heading).toUpperCase(),96,64,184);
      }
    }
    map.needsUpdate = true;
  }
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

// Solve the thigh and shin angles from the desired ankle position, keeping the stance foot near the floor.
function plantFoot(leg, forward, drop, outward, toe, alpha) {
  const thigh = .50, shin = .505;
  const reach = Math.min(thigh+shin-.004,Math.hypot(forward,drop));
  const bend = Math.acos(Math.max(-1,Math.min(1,(reach*reach-thigh*thigh-shin*shin)/(2*thigh*shin))));
  const hipAngle = -Math.atan2(forward,drop) - Math.atan2(shin*Math.sin(bend),thigh+shin*Math.cos(bend));
  leg.hip.rotation.x = blend(leg.hip.rotation.x,hipAngle,alpha);
  leg.hip.rotation.z = blend(leg.hip.rotation.z,outward,alpha);
  leg.knee.rotation.x = blend(leg.knee.rotation.x,bend,alpha);
  leg.foot.rotation.x = -leg.hip.rotation.x-leg.knee.rotation.x+toe;
  flexSkin(leg.skin,leg.knee.rotation.x);
}

function animateAthlete(athlete,t,alpha,dt,reduced,ball) {
  const {root,target,rig,body,face,arms,legs,index,pose} = athlete;
  const dx = target.x-root.position.x, dz = target.z-root.position.z;
  const step = Math.hypot(dx,dz)*(reduced ? 1 : alpha);
  const moving = !reduced && Math.hypot(dx,dz) > .13;
  const shooting = pose === 'shot';
  const turning = moving && !shooting;
  root.position.lerp(target,reduced ? 1 : alpha);
  if (moving) athlete.distance += step;
  const toward = turning ? Math.atan2(dx,dz) : athlete.facing*PI/2;
  const delta = Math.atan2(Math.sin(toward-root.rotation.y),Math.cos(toward-root.rotation.y));
  root.rotation.y += delta*(reduced ? 1 : Math.min(1,alpha*.80));
  const gait = Math.sin(athlete.distance*PI*2/1.66);
  const idle = reduced ? 0 : Math.sin(t*2.15+index*1.8);
  const ballAngle = ball && !reduced ? Math.atan2(ball.x-root.position.x,ball.z-root.position.z)-root.rotation.y : 0;
  const glance = Math.max(-.62,Math.min(.62,Math.atan2(Math.sin(ballAngle),Math.cos(ballAngle))));
  face.rotation.y = blend(face.rotation.y,shooting ? 0 : glance,alpha);
  face.rotation.x = blend(face.rotation.x,shooting ? -.13 : moving ? .045 : 0,alpha);

  const launch = reduced ? 0 : athlete.jump/24;
  const crouch = shooting ? .105-Math.min(.067,launch*.16) : moving ? .055 : athlete.actor ? .115 : .14;
  athlete.crouch = blend(athlete.crouch,crouch,reduced ? 1 : alpha);
  rig.position.y = launch-athlete.crouch + (moving ? Math.abs(gait)*.031 : idle*.009);
  body.rotation.x = blend(body.rotation.x,shooting ? -.055 : moving ? .19 : .13,alpha);
  body.rotation.z = moving ? gait*.068 : idle*.012;
  const dribble = athlete.actor && !shooting && !reduced && moving ? Math.sin(t*11.7) : 0;
  for (const [i,arm] of arms.entries()) {
    const {shoulder,elbow,wrist} = arm;
    const primary = i === athlete.dominant;
    const side = i ? 1 : -1;
    const stride = side*gait;
    const upper = shooting ? (primary ? -2.68 : -2.14) :
      athlete.actor && primary ? -.65+dribble*.26 : moving ? stride*.76 : -.44+idle*.035;
    shoulder.rotation.x = blend(shoulder.rotation.x,upper,alpha);
    shoulder.rotation.z = -side*(shooting ? (primary ? .09 : .28) : moving ? .13 : .33);
    elbow.rotation.x = blend(elbow.rotation.x,shooting ? (primary ? -.13 : -.70) :
      athlete.actor && primary ? -.67-dribble*.35 : moving ? -.83 : -.69,alpha);
    wrist.rotation.x = blend(wrist.rotation.x,shooting && primary ? -.54 : -.085+dribble*.16,alpha);
    flexSkin(arm.skin,elbow.rotation.x);
  }
  for (const [i,leg] of legs.entries()) {
    const phase = (athlete.distance/1.66+i*.5)%1;
    const swing = Math.max(0,(phase-.58)/.42);
    const stride = phase < .58 ? .40-phase/.58*.80 : -.40+swing*.80;
    const footLift = moving ? Math.sin(swing*PI)*.20 : 0;
    const forward = moving ? stride : (i ? .14 : -.105);
    const drop = 1.13-athlete.crouch-.09-footLift-(shooting ? launch*.08 : 0);
    const outward = (i ? 1 : -1)*(moving ? .038 : .115);
    plantFoot(leg,forward,drop,outward,moving ? Math.sin(swing*PI)*.22 : 0,reduced ? 1 : Math.min(1,alpha+dt*3));
  }
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

function createPhotoShadow() {
  const image = canvas(64,64);
  const ink = image.getContext('2d');
  const gradient = ink.createRadialGradient(32,32,2,32,32,31);
  gradient.addColorStop(0,'#130e1f83');
  gradient.addColorStop(1,'#130e1f00');
  ink.fillStyle = gradient;
  ink.fillRect(0,0,64,64);
  return new MeshBasicMaterial({map:texture(image),transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-1});
}

function setPhotoPlayer(athlete, atlas, shadowMaterial) {
  const playerArt = canvas(256,448), mirroredArt = canvas(256,448);
  const front = texture(playerArt), reverse = texture(mirroredArt);
  const material = new SpriteMaterial({map:front,transparent:true,alphaTest:.23,depthWrite:false,toneMapped:false});
  const portrait = new Sprite(material);
  portrait.center.set(.5,.025);
  portrait.scale.set(1.35,2.35,1);
  position(portrait,0,.035,0);
  athlete.root.add(portrait);
  const shadow = model(athlete.root,new PlaneGeometry(1.4,1.0),shadowMaterial,[1,1,1],[0,.023,0],false);
  shadow.rotation.x = -PI/2;
  athlete.photo = {portrait,material,front,reverse,playerArt,mirroredArt,atlas,shadow};
  athlete.rig.visible = false;
  paintPhotoPlayer(athlete);
}
function paintPhotoPlayer(athlete) {
  if (!athlete.photo) return;
  const {playerArt,mirroredArt,atlas,front,reverse} = athlete.photo;
  const variant = (athlete.index + Math.floor(athlete.index/5)*2)%5;
  const tileWidth = atlas.width/5;
  const [chestX,chestY] = [[65,84],[73,66],[48,99],[77,81],[79,74]][variant];
  for (const [image,flip] of [[playerArt,false],[mirroredArt,true]]) {
    const ctx = image.getContext('2d');
    ctx.clearRect(0,0,image.width,image.height);
    ctx.save();
    if (flip) {
      ctx.translate(image.width,0);
      ctx.scale(-1,1);
    }
    ctx.drawImage(atlas,variant*tileWidth,0,tileWidth,atlas.height,0,0,image.width,image.height);
    ctx.restore();
    if (athlete.number != null) {
      const x = (flip ? 128-chestX : chestX)*2;
      ctx.font = '900 31px system-ui';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineWidth = 4;
      ctx.strokeStyle = '#221731';
      ctx.fillStyle = '#fff9f1';
      ctx.strokeText(String(athlete.number),x,chestY*2);
      ctx.fillText(String(athlete.number),x,chestY*2);
    }
  }
  front.needsUpdate = true;
  reverse.needsUpdate = true;
}
function animatePhotoPlayer(athlete, t, alpha, reduced) {
  const {root,target,photo,index,pose,rig} = athlete;
  const distance = root.position.distanceTo(target);
  const moving = !reduced && distance > .12;
  athlete.distance += moving ? distance*alpha : 0;
  root.position.lerp(target,reduced ? 1 : alpha);
  const gait = Math.sin(athlete.distance*7+index);
  const idle = reduced ? 0 : Math.sin(t*2+index)*.009;
  const jump = reduced ? 0 : athlete.jump/24;
  const scale = rig.scale.y || 1;
  const flip = athlete.facing < 0;
  const map = flip ? photo.reverse : photo.front;
  if (photo.material.map !== map) photo.material.map = map;
  photo.portrait.position.y = .035 + jump + (moving ? Math.abs(gait)*.055 : idle);
  photo.portrait.scale.set(1.36*rig.scale.x,2.38*scale*(pose==='shot' ? 1.07 : 1),1);
  photo.material.rotation = moving ? (flip ? 1 : -1) * (.07 + gait*.035) : 0;
  photo.shadow.scale.setScalar(1-jump*.27);
  athlete.label.visible = athlete.actor && Boolean(athlete.identity);
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
  const players = Array.from({length:10},(_,index)=>buildAthlete(scene,index,jerseys[Math.floor(index/5)],teams[Math.floor(index/5)]));
  loadFaceMaterials().then(() => {
    if (!faceMaterials) return;
    for (const athlete of players) {
      applyFaceMaterial(athlete);
      athlete.beard.visible = false;
    }
  });
  const photoShadow = createPhotoShadow();
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
    for (const player of players) {
      if (player.photo) animatePhotoPlayer(player,playActive ? t : 0,pace,reduced);
      else animateAthlete(player,playActive ? t : 0,pace,dt,reduced,ball.visible ? ball.position : null);
    }
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
    photoKit(side,atlas) {
      for (const athlete of players.slice(side*5,side*5+5)) {
        if (!athlete.photo) setPhotoPlayer(athlete,atlas,photoShadow);
      }
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
