/** @type {AppTypes.Config} */

// DcmGet PDI serves this bundle, its private directory catalogue and DICOM
// objects from one 127.0.0.1 origin. Keep only the local directory source: no
// PACS, OIDC, sharing, cloud service, external CDN or demonstration endpoint.
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
  customizationService: {},
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
