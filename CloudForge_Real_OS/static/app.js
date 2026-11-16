(() => {
  const policySelect = document.getElementById('scheduler-select');
  const quantumControl = document.getElementById('rr-quantum-control');
  if (policySelect && quantumControl) {
    const updateQuantumVisibility = () => { quantumControl.hidden = policySelect.value !== 'Round Robin'; };
    policySelect.addEventListener('change', updateQuantumVisibility);
    updateQuantumVisibility();
  }
  // Poll real host and worker process counters on the authenticated dashboard.
  if (document.getElementById('host-cpu')) {
    const update = async () => {
      try {
        const response = await fetch('/api/system-metrics', { credentials: 'same-origin' });
        if (!response.ok) return;
        const data = await response.json();
        const set = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
        set('host-cpu', Math.round(data.cpu_percent));
        set('host-memory', Math.round(data.memory_percent));
        set('host-available', (data.memory_available / 1073741824).toFixed(1));
        set('managed-count', data.processes);
        set('process-rss', (data.process_rss / 1048576).toFixed(1));
        set('process-cpu', data.process_cpu_percent);
        set('queue-count', `${data.queued} waiting`);
        set('host-disk', Math.round(data.disk_percent));
        set('host-disk-free', (data.disk_free / 1073741824).toFixed(1));
        const cpuMeter = document.getElementById('host-cpu-meter');
        const ramMeter = document.getElementById('host-memory-meter');
        if (cpuMeter) cpuMeter.style.width = `${Math.min(100, data.cpu_percent)}%`;
        if (ramMeter) ramMeter.style.width = `${Math.min(100, data.memory_percent)}%`;
        const diskMeter = document.getElementById('host-disk-meter');
        if (diskMeter) diskMeter.style.width = `${Math.min(100, data.disk_percent)}%`;
      } catch (_) { /* Keep the last known local metrics if the server is restarting. */ }
    };
    update();
    window.setInterval(update, 3000);
  }
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const intro = document.getElementById('intro');
  const introKey = 'cloudforge-intro-seen-v3';

  if (intro) {
    const finishIntro = () => {
      intro.classList.add('intro-exit');
      try { sessionStorage.setItem(introKey, '1'); } catch (_) { /* Private browsing may block storage. */ }
      window.setTimeout(() => intro.remove(), 700);
    };
    const skip = document.getElementById('skip-intro');
    if (skip) skip.addEventListener('click', finishIntro, { once: true });
    let alreadySeen = false;
    try { alreadySeen = sessionStorage.getItem(introKey) === '1'; } catch (_) { /* Play the intro without storage. */ }
    if (reduced || alreadySeen) intro.remove();
    else window.setTimeout(finishIntro, 5200);
  }

  const progress = document.querySelector('.scroll-progress i');
  let scheduled = false;
  const updateScroll = () => {
    if (progress) {
      const span = document.documentElement.scrollHeight - window.innerHeight;
      progress.style.width = `${span > 0 ? (window.scrollY / span) * 100 : 0}%`;
    }
    document.documentElement.style.setProperty('--page-scroll', `${window.scrollY * -0.12}px`);
    scheduled = false;
  };
  window.addEventListener('scroll', () => {
    if (!scheduled) { scheduled = true; window.requestAnimationFrame(updateScroll); }
  }, { passive: true });
  updateScroll();

  if (!reduced && window.matchMedia('(pointer: fine)').matches) {
    window.addEventListener('pointermove', (event) => {
      document.documentElement.style.setProperty('--pointer-x', `${event.clientX}px`);
      document.documentElement.style.setProperty('--pointer-y', `${event.clientY}px`);
    }, { passive: true });
  }

  const revealItems = document.querySelectorAll('.hero-copy,.hero-visual,.feature-strip > div,.metric-card,.panel,.activity-row');
  if (!reduced && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries, activeObserver) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          activeObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -28px 0px' });
    revealItems.forEach((item, index) => {
      item.classList.add('reveal-ready');
      item.style.setProperty('--reveal-delay', `${Math.min(index % 5, 4) * 65}ms`);
      observer.observe(item);
    });
  }

  if (!reduced) {
    document.addEventListener('click', (event) => {
      const target = event.target.closest('button,.button,a');
      if (!target || target.matches(':disabled')) return;
      const bounds = target.getBoundingClientRect();
      const size = Math.max(bounds.width, bounds.height) * 1.35;
      const ripple = document.createElement('span');
      ripple.className = 'click-ripple';
      ripple.style.width = `${size}px`;
      ripple.style.height = `${size}px`;
      ripple.style.left = `${event.clientX - bounds.left - size / 2}px`;
      ripple.style.top = `${event.clientY - bounds.top - size / 2}px`;
      target.appendChild(ripple);
      window.setTimeout(() => ripple.remove(), 650);
    }, { passive: true });
  }
})();

// A dependency-free WebGL cloud globe. The CSS atmosphere remains as a graceful fallback.
(() => {
  const canvas = document.getElementById('cloud-globe');
  if (!canvas || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const gl = canvas.getContext('webgl', { alpha: false, antialias: false, powerPreference: 'low-power' });
  if (!gl) { canvas.remove(); return; }

  const vertexSource = `
    attribute vec2 a_position;
    varying vec2 v_uv;
    void main(){ v_uv=a_position*.5+.5; gl_Position=vec4(a_position,0.,1.); }
  `;
  const fragmentSource = `
    precision mediump float;
    varying vec2 v_uv;
    uniform vec2 u_resolution;
    uniform vec2 u_pointer;
    uniform float u_time;
    float hash3(vec3 p){p=fract(p*.1031);p+=dot(p,p.yxz+33.33);return fract((p.x+p.y)*p.z);}
    float noise3(vec3 p){
      vec3 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
      float a=hash3(i),b=hash3(i+vec3(1.,0.,0.));
      float c=hash3(i+vec3(0.,1.,0.)),d=hash3(i+vec3(1.,1.,0.));
      float e=hash3(i+vec3(0.,0.,1.)),f1=hash3(i+vec3(1.,0.,1.));
      float g=hash3(i+vec3(0.,1.,1.)),h=hash3(i+vec3(1.,1.,1.));
      return mix(mix(mix(a,b,f.x),mix(c,d,f.x),f.y),mix(mix(e,f1,f.x),mix(g,h,f.x),f.y),f.z);
    }
    float fbm(vec3 p){float value=0.,amp=.5;for(int i=0;i<4;i++){value+=amp*noise3(p);p=p*2.03+vec3(13.1,7.7,11.4);amp*=.5;}return value;}
    float hash21(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453123);}
    void main(){
      vec2 st=gl_FragCoord.xy/u_resolution;
      float aspect=u_resolution.x/u_resolution.y;
      vec2 p=(st-.5)*vec2(aspect,1.);
      vec3 sky=mix(vec3(.018,.029,.060),vec3(.025,.072,.115),smoothstep(-.65,.72,p.y));
      float haze=fbm(vec3(p*1.7,u_time*.012));
      sky+=vec3(.025,.045,.11)*smoothstep(.35,.78,haze)*smoothstep(-.7,.25,p.y);
      vec2 starCell=floor(gl_FragCoord.xy/vec2(97.,83.));
      vec2 starPoint=fract(gl_FragCoord.xy/vec2(97.,83.))-vec2(hash21(starCell),hash21(starCell+19.7));
      float star=1.-smoothstep(.012,.038,length(starPoint));
      float starSeed=hash21(starCell+5.3);
      sky+=vec3(.50,.72,1.)*star*step(.955,starSeed)*(.35+.65*sin(u_time*1.4+starSeed*30.));

      float radius=min(.44,aspect*.25);
      float centerX=min(aspect*.26,aspect*.5-radius-.025);
      vec2 q=p-vec2(centerX,.025+u_pointer.y*.012);
      q.x-=u_pointer.x*.025;
      float r=length(q);
      float outsideGlow=exp(-max(r-radius,0.)*30.)*(1.-smoothstep(radius,radius+.095,r));
      sky+=vec3(.05,.26,.50)*outsideGlow*1.35;
      float mask=1.-smoothstep(radius-.003,radius+.003,r);
      if(mask>.001){
        float z=sqrt(max(0.,1.-dot(q,q)/(radius*radius)));
        vec3 n=normalize(vec3(q/radius,z));
        float angle=u_time*.055+u_pointer.x*.12;
        vec3 cloudPos=vec3(n.x*cos(angle)+n.z*sin(angle),n.y,-n.x*sin(angle)+n.z*cos(angle));
        float broad=fbm(cloudPos*3.1+vec3(0.,u_time*.012,0.));
        float fine=fbm(cloudPos*8.2+vec3(-u_time*.009,0.,u_time*.006));
        float streak=fbm(cloudPos*13.5+vec3(0.,-u_time*.008,0.));
        float clouds=smoothstep(.40,.63,broad*.60+fine*.26+streak*.14);
        float softBands=smoothstep(.38,.68,fine*.62+broad*.38);
        float light=max(dot(n,normalize(vec3(-.55,.48,.72))),0.);
        float rim=pow(1.-max(n.z,0.),2.15);
        vec3 deep=vec3(.035,.070,.155)*(0.65+light*.82);
        vec3 cloudColor=mix(vec3(.30,.57,.85),vec3(.91,.97,1.),.22+light*.68);
        vec3 orb=mix(deep,cloudColor,clamp(clouds*.98+softBands*.20,0.,1.));
        orb+=vec3(.12,.40,.72)*rim*1.05;
        orb+=cloudColor*pow(clouds,2.)*.25;
        sky=mix(sky,orb,mask);
        sky+=vec3(.16,.48,.82)*exp(-abs(r-radius)*72.)*.42;
      }
      gl_FragColor=vec4(sky,1.);
    }
  `;

  const compile = (type, source) => {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    return shader;
  };
  try {
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'a_position');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const resolution = gl.getUniformLocation(program, 'u_resolution');
    const time = gl.getUniformLocation(program, 'u_time');
    const pointer = gl.getUniformLocation(program, 'u_pointer');
    const pointerState = { x: 0, y: 0 };
    window.addEventListener('pointermove', (event) => {
      pointerState.x = (event.clientX / Math.max(window.innerWidth, 1) - .5) * 2;
      pointerState.y = (event.clientY / Math.max(window.innerHeight, 1) - .5) * 2;
    }, { passive: true });
    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 1.15);
      const width = Math.max(1, Math.floor(window.innerWidth * ratio));
      const height = Math.max(1, Math.floor(window.innerHeight * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width; canvas.height = height; gl.viewport(0, 0, width, height);
      }
      gl.uniform2f(resolution, width, height);
    };
    window.addEventListener('resize', resize, { passive: true });
    resize();
    document.documentElement.classList.add('webgl-active');
    canvas.dataset.renderer = 'webgl';
    const started = performance.now();
    const frame = (now) => {
      resize();
      gl.uniform1f(time, (now - started) / 1000);
      gl.uniform2f(pointer, pointerState.x, pointerState.y);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      window.requestAnimationFrame(frame);
    };
    window.requestAnimationFrame(frame);
  } catch (_) {
    canvas.remove();
  }
})();
