<%inherit file="../${context.get('request').registry.settings.get('clld.app_template', 'app.mako')}"/>
<%namespace name="util" file="../util.mako"/>


<h2>${_('Contribution')} ${ctx.name}</h2>

<ol>
% for k, v in ctx.datadict().items():
<dt>${k}</dt>
<dd>${v}</dd>
% endfor
</ol>