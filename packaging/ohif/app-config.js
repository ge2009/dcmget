/** @type {AppTypes.Config} */

// DcmGet PDI serves this bundle, its private directory catalogue and DICOM
// objects from one 127.0.0.1 origin. Keep only the local directory source: no
// PACS, OIDC, sharing, cloud service, external CDN or demonstration endpoint.

// OHIF 3.12.6 only wires a series thumbnail's double-click handler.  Keep the
// pinned bundle immutable and adapt the local PDI interaction at the document
// boundary so one click loads the series, matching the WViewers workflow.
(function installSingleClickSeriesLoading() {
  const installationKey = '__dcmgetSingleClickSeriesLoadingInstalled';
  const thumbnailSelector =
    '[data-cy="study-browser-thumbnail"], [data-cy="study-browser-thumbnail-no-image"]';
  const ignoredDescendantSelector =
    'button, a, input, select, textarea, [role="menuitem"]';

  if (typeof window === 'undefined' || typeof document === 'undefined' || window[installationKey]) {
    return;
  }
  window[installationKey] = true;

  const lastActivation = new WeakMap();
  document.addEventListener(
    'click',
    function (event) {
      const target = event.target;
      if (!(target instanceof Element) || event.defaultPrevented) {
        return;
      }
      const thumbnail = target.closest(thumbnailSelector);
      if (!thumbnail || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) {
        return;
      }
      const interactiveDescendant = target.closest(ignoredDescendantSelector);
      if (interactiveDescendant && interactiveDescendant !== thumbnail) {
        return;
      }
      // A physical double-click starts with detail=1 and then detail=2.  The
      // first click has already loaded the series, so don't load it twice.
      if (event.detail > 1) {
        return;
      }

      lastActivation.set(thumbnail, Date.now());
      const activationEvent = new MouseEvent('dblclick', {
        bubbles: true,
        cancelable: true,
        view: window,
        button: 0,
        buttons: 1,
        clientX: event.clientX,
        clientY: event.clientY,
      });
      activationEvent.__dcmgetSingleClickSeries = true;
      thumbnail.dispatchEvent(activationEvent);
    },
    true
  );

  document.addEventListener(
    'dblclick',
    function (event) {
      if (event.__dcmgetSingleClickSeries) {
        return;
      }
      const target = event.target;
      const thumbnail = target instanceof Element ? target.closest(thumbnailSelector) : null;
      const activatedAt = thumbnail ? lastActivation.get(thumbnail) : 0;
      if (activatedAt && Date.now() - activatedAt < 500) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true
  );
})();

function formatDicomTime(value) {
  const rawValue = String(value || '').replace(/[^\d.]/g, '');
  if (rawValue.length < 6) {
    return '';
  }
  return `${rawValue.slice(0, 2)}:${rawValue.slice(2, 4)}:${rawValue.slice(4, 6)}`;
}

const viewportOverlayCustomizations = {
  'viewportOverlay.topLeft': [
    {
      id: 'PatientName',
      inheritsFrom: 'ohif.overlayItem',
      label: '姓名:',
      title: '患者姓名',
      condition: ({ referenceInstance }) => referenceInstance?.PatientName,
      contentF: ({ referenceInstance, formatters: { formatPN } }) =>
        formatPN(referenceInstance.PatientName),
    },
    {
      id: 'PatientID',
      inheritsFrom: 'ohif.overlayItem',
      label: '患者ID:',
      title: '患者 ID',
      condition: ({ referenceInstance }) => referenceInstance?.PatientID,
      contentF: ({ referenceInstance }) => referenceInstance.PatientID,
    },
    {
      id: 'AccessionNumber',
      inheritsFrom: 'ohif.overlayItem',
      label: '检查号:',
      title: '检查号',
      condition: ({ referenceInstance }) => referenceInstance?.AccessionNumber,
      contentF: ({ referenceInstance }) => referenceInstance.AccessionNumber,
    },
    {
      id: 'PatientAgeSex',
      inheritsFrom: 'ohif.overlayItem',
      label: '',
      title: '年龄与性别',
      condition: ({ referenceInstance }) =>
        referenceInstance?.PatientAge || referenceInstance?.PatientSex,
      contentF: ({ referenceInstance }) =>
        [referenceInstance.PatientAge, referenceInstance.PatientSex].filter(Boolean).join(' / '),
    },
  ],
  'viewportOverlay.topRight': [
    {
      id: 'InstitutionName',
      inheritsFrom: 'ohif.overlayItem',
      label: '',
      title: '机构名称',
      condition: ({ referenceInstance }) => referenceInstance?.InstitutionName,
      contentF: ({ referenceInstance }) => referenceInstance.InstitutionName,
    },
    {
      id: 'StudyDateTime',
      inheritsFrom: 'ohif.overlayItem',
      label: '',
      title: '检查日期与时间',
      condition: ({ referenceInstance }) =>
        referenceInstance?.StudyDate || referenceInstance?.StudyTime,
      contentF: ({ referenceInstance, formatters: { formatDate } }) =>
        [formatDate(referenceInstance.StudyDate), formatDicomTime(referenceInstance.StudyTime)]
          .filter(Boolean)
          .join(' '),
    },
    {
      id: 'Manufacturer',
      inheritsFrom: 'ohif.overlayItem',
      label: '',
      title: '设备厂商',
      condition: ({ referenceInstance }) => referenceInstance?.Manufacturer,
      contentF: ({ referenceInstance }) => referenceInstance.Manufacturer,
    },
  ],
  'viewportOverlay.bottomLeft': [
    {
      id: 'WindowLevel',
      inheritsFrom: 'ohif.overlayItem.windowLevel',
      title: '窗宽窗位',
    },
    {
      id: 'PixelSpacing',
      inheritsFrom: 'ohif.overlayItem',
      label: '间距:',
      title: '像素间距',
      condition: ({ instance, referenceInstance }) =>
        (instance?.PixelSpacing ?? referenceInstance?.PixelSpacing)?.length,
      contentF: ({ instance, referenceInstance, formatters: { formatNumberPrecision } }) => {
        const pixelSpacing = instance?.PixelSpacing ?? referenceInstance?.PixelSpacing;
        return `${formatNumberPrecision(pixelSpacing[0], 3)} x ${formatNumberPrecision(
          pixelSpacing[1],
          3
        )} mm`;
      },
    },
    {
      id: 'SliceThickness',
      inheritsFrom: 'ohif.overlayItem',
      label: '层厚:',
      title: '层厚',
      condition: ({ instance, referenceInstance }) => {
        const thickness = Number(instance?.SliceThickness ?? referenceInstance?.SliceThickness);
        return Number.isFinite(thickness) && thickness > 0 && thickness < 100;
      },
      contentF: ({ instance, referenceInstance, formatters: { formatNumberPrecision } }) =>
        `${formatNumberPrecision(
          instance?.SliceThickness ?? referenceInstance?.SliceThickness,
          3
        )} mm`,
    },
    {
      id: 'ZoomLevel',
      inheritsFrom: 'ohif.overlayItem.zoomLevel',
      condition: props =>
        props.toolGroupService.getActiveToolForViewport(props.viewportId) === 'Zoom',
    },
  ],
  'viewportOverlay.bottomRight': [
    {
      id: 'SeriesNumber',
      inheritsFrom: 'ohif.overlayItem',
      label: '序列:',
      title: '序列号',
      condition: ({ referenceInstance }) => referenceInstance?.SeriesNumber !== undefined,
      contentF: ({ referenceInstance }) => referenceInstance.SeriesNumber,
    },
    {
      id: 'InstanceNumber',
      inheritsFrom: 'ohif.overlayItem.instanceNumber',
      title: '图像序号',
    },
    {
      id: 'TransferSyntaxUID',
      inheritsFrom: 'ohif.overlayItem',
      label: 'TS:',
      title: '传输语法',
      condition: ({ instance, referenceInstance }) =>
        instance?.TransferSyntaxUID || referenceInstance?.TransferSyntaxUID,
      contentF: ({ instance, referenceInstance }) =>
        instance?.TransferSyntaxUID || referenceInstance?.TransferSyntaxUID,
    },
  ],
};

window.config = {
  name: 'DcmGet PDI',
  routerBasename: null,
  whiteLabeling: {
    createLogoComponentFn: function (React) {
      return React.createElement(
        'div',
        { className: 'flex h-12 items-center gap-2' },
        React.createElement('img', {
          className: 'h-9 w-9 object-contain',
          src: '/assets/dcmget-logo.png',
          alt: 'DcmGet',
        }),
        React.createElement(
          'span',
          { className: 'text-lg font-semibold tracking-wide text-white' },
          'DcmGet PDI'
        )
      );
    },
  },
  extensions: [],
  modes: [],
  customizationService: viewportOverlayCustomizations,
  showStudyList: false,
  maxNumberOfWebWorkers: (function () {
    try {
      const compact =
        /Android|iPhone|iPod|iPad|Mobile|MicroMessenger|WeChat/i.test(
          navigator.userAgent || ''
        ) || window.matchMedia('(max-width: 900px)').matches;
      return compact ? 3 : 6;
    } catch (_error) {
      return 3;
    }
  })(),
  showWarningMessageForCrossOrigin: false,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  strictZSpacingForVolumeViewport: true,
  investigationalUseDialog: { option: 'never' },
  groupEnabledModesFirst: true,
  showErrorDetails: 'dev',
  maxNumRequests: {
    interaction: 12,
    thumbnail: 5,
    prefetch: 6,
  },
  defaultDataSourceName: 'directory',
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomjson',
      sourceName: 'directory',
      configuration: {
        friendlyName: 'DcmGet PDI 本地影像',
        name: 'directory',
      },
    },
  ],
};
