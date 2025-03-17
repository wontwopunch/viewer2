// OpenSeadragon SVG Overlay plugin
(function() {
    if (!window.OpenSeadragon) {
        console.error('[openseadragon-svg-overlay] requires OpenSeadragon');
        return;
    }

    OpenSeadragon.Viewer.prototype.svgOverlay = function() {
        if (this._svgOverlay) {
            return this._svgOverlay;
        }

        this._svgOverlay = new Overlay(this);
        return this._svgOverlay;
    };

    // ----------
    var Overlay = function(viewer) {
        var self = this;

        this._viewer = viewer;
        this._containerWidth = 0;
        this._containerHeight = 0;

        this._svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this._svg.style.position = 'absolute';
        this._svg.style.left = 0;
        this._svg.style.top = 0;
        this._svg.style.width = '100%';
        this._svg.style.height = '100%';
        this._svg.style.pointerEvents = 'none'; // 클릭 이벤트가 통과하도록 설정
        this._viewer.canvas.appendChild(this._svg);

        this._node = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        this._svg.appendChild(this._node);

        // 리사이즈 호출 제한(throttle) 적용
        let resizeTimeout = null;
        const throttledResize = function() {
            if (resizeTimeout) {
                clearTimeout(resizeTimeout);
            }
            resizeTimeout = setTimeout(function() {
                self.resize();
                resizeTimeout = null;
            }, 50); // 50ms 지연
        };

        // 필수 이벤트만 연결하여 성능 향상
        this._viewer.addHandler('animation-finish', function() {
            self.resize();
        });

        this._viewer.addHandler('open', function() {
            self.resize();
        });

        this._viewer.addHandler('rotate', function() {
            self.resize();
        });

        this._viewer.addHandler('resize', function() {
            throttledResize();
        });

        this._viewer.addHandler('viewport-change', function() {
            throttledResize();
        });

        this.resize();
    };

    // ----------
    Overlay.prototype = {
        node: function() {
            return this._node;
        },

        resize: function() {
            if (this._containerWidth !== this._viewer.container.clientWidth) {
                this._containerWidth = this._viewer.container.clientWidth;
                this._svg.setAttribute('width', this._containerWidth);
            }

            if (this._containerHeight !== this._viewer.container.clientHeight) {
                this._containerHeight = this._viewer.container.clientHeight;
                this._svg.setAttribute('height', this._containerHeight);
            }

            // 개선된 변환 계산
            if (this._viewer.viewport) {
                var p = this._viewer.viewport.pixelFromPoint(new OpenSeadragon.Point(0, 0), true);
                var zoom = this._viewer.viewport.getZoom(true);
                var rotation = this._viewer.viewport.getRotation();
                
                // 더 정확한 위치를 위한 매트릭스 변환 사용
                var transform = 
                    'translate(' + p.x + ',' + p.y + ') ' +
                    'scale(' + zoom + ') ' +
                    'rotate(' + rotation + ')';
                
                this._node.setAttribute('transform', transform);
            }
        },

        onClick: function(node, handler) {
            // 클릭 가능하도록 설정
            node.style.pointerEvents = 'auto';
            node.addEventListener('click', handler);
        },

        // 모든 어노테이션을 지우는 메서드 추가
        clear: function() {
            while (this._node.firstChild) {
                this._node.removeChild(this._node.firstChild);
            }
        }
    };
})();